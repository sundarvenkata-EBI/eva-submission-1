import csv
import os
import shutil
import subprocess
from pathlib import Path

import yaml
from cached_property import cached_property
from ebi_eva_common_pyutils import command_utils
from ebi_eva_common_pyutils.config import cfg
from ebi_eva_common_pyutils.config_utils import get_mongo_uri_for_eva_profile, get_primary_mongo_creds_for_profile, \
    get_accession_pg_creds_for_profile, get_count_service_creds_for_profile
from ebi_eva_common_pyutils.ena_utils import get_assembly_name_and_taxonomy_id
from ebi_eva_common_pyutils.metadata_utils import resolve_variant_warehouse_db_name, insert_new_assembly_and_taxonomy, \
    get_assembly_set_from_metadata
from ebi_eva_common_pyutils.pg_utils import get_all_results_for_query, execute_query
from ebi_eva_common_pyutils.spring_properties import SpringPropertiesGenerator

from eva_submission import NEXTFLOW_DIR
from eva_submission.eload_submission import Eload
from eva_submission.eload_utils import provision_new_database_for_variant_warehouse
from eva_submission.submission_config import EloadConfig
from eva_submission.vep_utils import get_vep_and_vep_cache_version
from eva_submission.ingestion_templates import accession_props_template, variant_load_props_template

project_dirs = {
    'logs': '00_logs',
    'valid': '30_eva_valid',
    'transformed': '40_transformed',
    'stats': '50_stats',
    'annotation': '51_annotation',
    'accessions': '52_accessions',
    'clustering': '53_clustering',
    'public': '60_eva_public',
    'external': '70_external_submissions',
    'deprecated': '80_deprecated'
}


class EloadIngestion(Eload):
    config_section = 'ingestion'  # top-level config key
    all_tasks = ['metadata_load', 'accession', 'variant_load', 'annotation', 'optional_remap_and_cluster']
    nextflow_complete_value = '<complete>'

    def __init__(self, eload_number, config_object: EloadConfig = None):
        super().__init__(eload_number, config_object)
        self.project_accession = self.eload_cfg.query('brokering', 'ena', 'PROJECT')
        self.taxonomy = self.eload_cfg.query('submission', 'taxonomy_id')
        self.private_settings_file = cfg['maven']['settings_file']
        self.maven_profile = cfg['maven']['environment']
        self.mongo_uri = get_mongo_uri_for_eva_profile(self.maven_profile, self.private_settings_file)
        self.properties_generator = SpringPropertiesGenerator(self.maven_profile, self.private_settings_file)

    def ingest(
            self,
            instance_id=None,
            clustering_instance_id=None,
            tasks=None,
            vep_cache_assembly_name=None,
            resume=False
    ):
        self.eload_cfg.set(self.config_section, 'ingestion_date', value=self.now)
        self.project_dir = self.setup_project_dir()
        # Pre ingestion checks
        self.check_aggregation_done()
        self.check_brokering_done()
        self.check_variant_db()

        if not tasks:
            tasks = self.all_tasks

        if 'metadata_load' in tasks:
            self.load_from_ena()
            # Update analysis in the metadata in case the perl script failed (usually because the project already exist)
            self.update_assembly_set_in_analysis()
        do_accession = 'accession' in tasks
        do_variant_load = 'variant_load' in tasks
        annotation_only = 'annotation' in tasks and not do_variant_load

        if do_accession or do_variant_load or annotation_only:
            self.fill_vep_versions(vep_cache_assembly_name)
            vcf_files_to_ingest = self._generate_csv_mappings_to_ingest()

        if do_accession:
            self.eload_cfg.set(self.config_section, 'accession', 'instance_id', value=instance_id)
            self.update_config_with_hold_date(self.project_accession)
            self.run_accession_workflow(vcf_files_to_ingest, resume=resume)
            self.insert_browsable_files()
            self.update_browsable_files_with_date()
            self.update_files_with_ftp_path()
            self.refresh_study_browser()

        if 'optional_remap_and_cluster' in tasks:
            self.eload_cfg.set(self.config_section, 'clustering', 'instance_id', value=clustering_instance_id)
            target_assembly = self._get_target_assembly()
            # EVA-3207: Temporary limitation while we sort out the remapping across species
            if target_assembly and self._target_assembly_from_same_taxonomy(target_assembly):
                self.run_remap_and_cluster_workflow(target_assembly, resume=resume)

        if do_variant_load or annotation_only:
            self.run_variant_load_workflow(vcf_files_to_ingest, annotation_only, resume=resume)
            self.update_loaded_assembly_in_browsable_files()

    def fill_vep_versions(self, vep_cache_assembly_name=None):
        analyses = self.eload_cfg.query('brokering', 'analyses')
        for analysis_alias, analysis_data in analyses.items():
            assembly_accession = analysis_data['assembly_accession']
            if ('vep' in self.eload_cfg.query(self.config_section)
                    and assembly_accession in self.eload_cfg.query(self.config_section, 'vep')):
                continue
            vep_version, vep_cache_version, vep_species = get_vep_and_vep_cache_version(
                self.mongo_uri,
                self.eload_cfg.query(self.config_section, 'database', assembly_accession, 'db_name'),
                assembly_accession,
                vep_cache_assembly_name
            )
            self.eload_cfg.set(self.config_section, 'vep', assembly_accession, 'version', value=vep_version)
            self.eload_cfg.set(self.config_section, 'vep', assembly_accession, 'cache_version', value=vep_cache_version)
            self.eload_cfg.set(self.config_section, 'vep', assembly_accession, 'species', value=vep_species)

    def _get_vcf_files_from_brokering(self):
        vcf_files = []
        analyses = self.eload_cfg.query('brokering', 'analyses')
        if analyses:
            for analysis_alias, analysis_data in analyses.items():
                files = analysis_data['vcf_files']
                vcf_files.extend(files) if files else None
            return vcf_files

    def check_brokering_done(self):
        vcf_files = self._get_vcf_files_from_brokering()
        if not vcf_files:
            self.error('No brokered VCF files found, aborting ingestion.')
            raise ValueError('No brokered VCF files found.')
        if self.project_accession is None:
            self.error('No project accession in submission config, check that brokering to ENA is done. ')
            raise ValueError('No project accession in submission config.')
        # check there are no vcfs in valid folder that aren't in brokering config
        for valid_vcf in self.valid_vcf_filenames:
            if not any(f.endswith(valid_vcf.name) for f in vcf_files):
                raise ValueError(f'Found {valid_vcf} in valid folder that was not in brokering config')

    def check_aggregation_done(self):
        errors = []
        for analysis_alias, analysis_acc in self.eload_cfg.query('brokering', 'ena', 'ANALYSIS').items():
            aggregation = self.eload_cfg.query('validation', 'aggregation_check', 'analyses', analysis_alias)
            if aggregation is None:
                error = f'Aggregation type was not determined during validation for {analysis_alias}'
                self.error(error)
                errors.append(error)
            else:
                self.eload_cfg.set(self.config_section, 'aggregation', analysis_acc, value=aggregation)
        if errors:
            raise ValueError(f'{len(errors)} analysis have not set the aggregation. '
                             f'Rerun the validation with --validation_tasks aggregation_check')

    def get_db_name(self, assembly_accession):
        """
        Constructs the expected database name in mongo, based on assembly info retrieved from EVAPRO.
        """
        # query EVAPRO for db name based on taxonomy id and accession
        with self.metadata_connection_handle as conn:
            db_name = resolve_variant_warehouse_db_name(conn, assembly_accession, self.taxonomy)
            if not db_name:
                raise ValueError(f'Database name for taxid:{self.taxonomy} and assembly {assembly_accession} '
                                 f'could not be retrieved or constructed')
        return db_name

    def _get_assembly_accessions(self):
        assembly_accessions = set()
        analyses = self.eload_cfg.query('submission', 'analyses')
        for analysis_alias, analysis_data in analyses.items():
            assembly_accessions.add(analysis_data['assembly_accession'])
        return assembly_accessions

    def check_variant_db(self):
        """
        Checks mongo for the right variant database.
        Retrieve the database from EVAPRO based on the assembly accession and taxonomy or
        Construct it from the species scientific name and assembly name
        """
        assembly_accessions = self._get_assembly_accessions()
        assembly_to_db_name = {}
        # Find the database names
        for assembly_accession in assembly_accessions:
            db_name_retrieved = self.get_db_name(assembly_accession)
            assembly_to_db_name[assembly_accession] = {'db_name': db_name_retrieved}

        self.eload_cfg.set(self.config_section, 'database', value=assembly_to_db_name)

        # insert the database names components if they're not already in the metadata
        with self.metadata_connection_handle as conn:
            for assembly, db in assembly_to_db_name.items():
                # warns but doesn't crash if assembly set already exists
                insert_new_assembly_and_taxonomy(
                    metadata_connection_handle=conn,
                    assembly_accession=assembly,
                    taxonomy_id=self.taxonomy,
                )

        for db_info in assembly_to_db_name.values():
            provision_new_database_for_variant_warehouse(db_info['db_name'])

    def load_from_ena(self):
        if self.eload_cfg.query('brokering', 'ena', 'existing_project'):
            analyses = self.eload_cfg.query('brokering', 'ena', 'ANALYSIS')
            for analysis_accession in analyses.values():
                self.load_from_ena_from_project_or_analysis(analysis_accession)

        else:
            self.load_from_ena_from_project_or_analysis()

    def load_from_ena_from_project_or_analysis(self, analysis_accession=None):
        """
        Loads Analysis metadata from ENA into EVAPRO to the project associated with this ELOAD or to an analysis
        if it is specified.
        """
        # Current submission process never changes -c or -v
        command = (f"perl {cfg['executable']['load_from_ena']} -p {self.project_accession} -c submitted -v 1 "
                   f"-l {self._get_dir('scratch')} -e {str(self.eload_num)}")
        if analysis_accession:
            command += f' -A -a {analysis_accession}'
        try:
            command_utils.run_command_with_output('Load metadata from ENA to EVAPRO', command)
            self.eload_cfg.set(self.config_section, 'ena_load', value='success')
        except subprocess.CalledProcessError as e:
            self.error('ENA metadata load failed: aborting ingestion.')
            self.eload_cfg.set(self.config_section, 'ena_load', value='failure')
            raise e

    def _copy_file(self, source_path, target_dir):
        target_path = target_dir.joinpath(source_path.name)
        if not target_path.exists():
            shutil.copyfile(source_path, target_path)
        else:
            self.warning(f'{source_path.name} already exists in {target_dir}, not copying.')

    def setup_project_dir(self):
        """
        Sets up project directory and copies VCF files from the eload directory.
        """
        project_dir = Path(cfg['projects_dir'], self.project_accession)
        os.makedirs(project_dir, exist_ok=True)
        for v in project_dirs.values():
            os.makedirs(project_dir.joinpath(v), exist_ok=True)
        # copy valid vcfs + index to 'valid' folder and 'public' folder
        valid_dir = project_dir.joinpath(project_dirs['valid'])
        public_dir = project_dir.joinpath(project_dirs['public'])
        analyses = self.eload_cfg.query('brokering', 'analyses')
        for analysis_alias, analysis_data in analyses.items():
            for vcf_file, vcf_file_info in analysis_data['vcf_files'].items():
                vcf_path = Path(vcf_file)
                self._copy_file(vcf_path, valid_dir)
                self._copy_file(vcf_path, public_dir)
                csi_path = Path(vcf_file_info['csi'])
                self._copy_file(csi_path, valid_dir)
                self._copy_file(csi_path, public_dir)
        self.eload_cfg.set(self.config_section, 'project_dir', value=str(project_dir))
        return project_dir

    def get_study_name(self):
        with self.metadata_connection_handle as conn:
            query = f"SELECT title FROM evapro.project WHERE project_accession='{self.project_accession}';"
            rows = get_all_results_for_query(conn, query)
        if len(rows) != 1:
            raise ValueError(f'More than one project with accession {self.project_accession} found in metadata DB.')
        return rows[0][0]

    def _generate_csv_mappings_to_ingest(self):
        vcf_files_to_ingest = os.path.join(self.eload_dir, 'vcf_files_to_ingest.csv')
        with open(vcf_files_to_ingest, 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['vcf_file', 'assembly_accession', 'fasta', 'report', 'analysis_accession', 'db_name',
                             'vep_version', 'vep_cache_version', 'vep_species', 'aggregation'])
            analyses = self.eload_cfg.query('brokering', 'analyses')
            for analysis_alias, analysis_data in analyses.items():
                assembly_accession = analysis_data['assembly_accession']
                fasta = analysis_data['assembly_fasta']
                report = analysis_data['assembly_report']
                analysis_accession = self.eload_cfg.query('brokering', 'ena', 'ANALYSIS', analysis_alias)
                db_name = self.eload_cfg.query(self.config_section, 'database', assembly_accession, 'db_name')
                vep_version = self.eload_cfg.query(self.config_section, 'vep', assembly_accession, 'version')
                vep_cache_version = self.eload_cfg.query(self.config_section, 'vep', assembly_accession, 'cache_version')
                vep_species = self.eload_cfg.query(self.config_section, 'vep', assembly_accession, 'species')
                if not vep_version or not vep_cache_version:
                    vep_version = ''
                    vep_cache_version = ''
                    vep_species = ''
                aggregation = self.eload_cfg.query(self.config_section, 'aggregation', analysis_accession)
                if analysis_data['vcf_files']:
                    for vcf_file in analysis_data['vcf_files']:
                        writer.writerow([vcf_file, assembly_accession, fasta, report, analysis_accession, db_name,
                                         vep_version, vep_cache_version, vep_species, aggregation])
                else:
                    self.warning(f"VCF files for analysis {analysis_alias} not found")
        return vcf_files_to_ingest

    def run_accession_workflow(self, vcf_files_to_ingest, resume):
        mongo_host, mongo_user, mongo_pass = get_primary_mongo_creds_for_profile(self.maven_profile, self.private_settings_file)
        pg_url, pg_user, pg_pass = get_accession_pg_creds_for_profile(self.maven_profile, self.private_settings_file)
        counts_url, counts_user, counts_pass = get_count_service_creds_for_profile(self.maven_profile, self.private_settings_file)
        job_props = accession_props_template(
            taxonomy_id=self.taxonomy,
            project_accession=self.project_accession,
            instance_id=self.eload_cfg.query(self.config_section, 'accession', 'instance_id'),
            mongo_host=mongo_host,
            mongo_user=mongo_user,
            mongo_pass=mongo_pass,
            postgres_url=pg_url,
            postgres_user=pg_user,
            postgres_pass=pg_pass,
            counts_url=counts_url,
            counts_user=counts_user,
            counts_pass=counts_pass
        )
        accession_config = {
            'valid_vcfs': vcf_files_to_ingest,
            'project_accession': self.project_accession,
            'instance_id': self.eload_cfg.query(self.config_section, 'accession', 'instance_id'),
            'accession_job_props': job_props,
            'public_ftp_dir': cfg['public_ftp_dir'],
            'accessions_dir': os.path.join(self.project_dir, project_dirs['accessions']),
            'public_dir': os.path.join(self.project_dir, project_dirs['public']),
            'logs_dir': os.path.join(self.project_dir, project_dirs['logs']),
            'executable': cfg['executable'],
            'jar': cfg['jar'],
        }
        self.run_nextflow('accession', accession_config, resume)

    def run_variant_load_workflow(self, vcf_files_to_ingest, annotation_only, resume):
        job_props = variant_load_props_template(
                project_accession=self.project_accession,
                study_name=self.get_study_name(),
                output_dir=self.project_dir.joinpath(project_dirs['transformed']),
                annotation_dir=self.project_dir.joinpath(project_dirs['annotation']),
                stats_dir=self.project_dir.joinpath(project_dirs['stats']),
        )
        load_config = {
            'valid_vcfs': vcf_files_to_ingest,
            'vep_path': cfg['vep_path'],
            'load_job_props': job_props,
            'acc_import_job_props': {'db.collections.variants.name': 'variants_2_0'},
            'project_accession': self.project_accession,
            'project_dir': str(self.project_dir),
            'logs_dir': os.path.join(self.project_dir, project_dirs['logs']),
            'eva_pipeline_props': cfg['eva_pipeline_props'],
            'executable': cfg['executable'],
            'jar': cfg['jar'],
            'annotation_only': annotation_only,
        }
        self.run_nextflow('variant_load', load_config, resume)

    def run_remap_and_cluster_workflow(self, target_assembly, resume):
        clustering_instance = self.eload_cfg.query(self.config_section, 'clustering', 'instance_id')
        scientific_name = self.eload_cfg.query('submission', 'scientific_name')
        # this is where all the output will get stored - logs, properties, work dirs...
        output_dir = os.path.join(self.project_dir, project_dirs['clustering'])

        source_asms = self._get_assembly_accessions()
        extraction_properties_file = self.create_extraction_properties(
            output_file_path=os.path.join(output_dir, 'remapping_extraction.properties'),
            taxonomy=self.taxonomy
        )
        ingestion_properties_file = self.create_ingestion_properties(
            output_file_path=os.path.join(output_dir, 'remapping_ingestion.properties'),
            target_assembly=target_assembly
        )
        clustering_template_file = self.create_clustering_properties(
            output_file_path=os.path.join(output_dir, 'clustering_template.properties'),
            clustering_instance=clustering_instance,
            target_assembly=target_assembly
        )

        remap_cluster_config = {
            'taxonomy_id': self.taxonomy,
            'source_assemblies': source_asms,
            'target_assembly_accession': target_assembly,
            'species_name': scientific_name,
            'output_dir': output_dir,
            'genome_assembly_dir': cfg['genome_downloader']['output_directory'],
            'extraction_properties': extraction_properties_file,
            'ingestion_properties': ingestion_properties_file,
            'clustering_properties': clustering_template_file,
            'clustering_instance': clustering_instance,
            'remapping_config': cfg.config_file
        }
        for part in ['executable', 'nextflow', 'jar']:
            remap_cluster_config[part] = cfg[part]
        self.run_nextflow('remap_and_cluster', remap_cluster_config, resume)

    def _get_target_assembly(self):
        if self.taxonomy == 9606:
            self.info('No remapping or clustering for human studies')
            return None
        with self.metadata_connection_handle as conn:
            current_query = (
                f"SELECT assembly_id FROM evapro.supported_assembly_tracker "
                f"WHERE taxonomy_id={self.taxonomy} AND current=true;"
            )
            results = get_all_results_for_query(conn, current_query)
            if len(results) > 0:
                self.eload_cfg.set(self.config_section, 'remap_and_cluster', 'target_assembly', value=results[0][0])
                return results[0][0]
        self.warning(f'Could not find any current supported assembly for {self.taxonomy}, skipping clustering')
        return None

    def _target_assembly_from_same_taxonomy(self, target_assembly):
        # Find taxonomy of the target assembly
        _, taxonomy_id_from_target = get_assembly_name_and_taxonomy_id(target_assembly)
        if int(taxonomy_id_from_target) != int(self.taxonomy):
            self.warning(f'Target assembly {target_assembly} is from a different taxonomy {taxonomy_id_from_target} '
                         f'compared to the current project {self.taxonomy}. Therefore remapping will not be carried out!')
            return False
        return True

    def create_extraction_properties(self, output_file_path, taxonomy):
        properties = self.properties_generator.get_remapping_extraction_properties(
            taxonomy=taxonomy,
            projects=self.project_accession
        )
        with open(output_file_path, 'w') as open_file:
            open_file.write(properties)
        return output_file_path

    def create_ingestion_properties(self, output_file_path, target_assembly):
        properties = self.properties_generator.get_remapping_ingestion_properties(
            target_assembly=target_assembly,
            load_to='EVA'
        )
        with open(output_file_path, 'w') as open_file:
            open_file.write(properties)
        return output_file_path

    def create_clustering_properties(self, output_file_path, clustering_instance, target_assembly):
        properties = self.properties_generator.get_clustering_properties(
            instance=clustering_instance,
            target_assembly=target_assembly,
            projects=self.project_accession,
            rs_report_path=f'{target_assembly}_rs_report.txt'
        )
        with open(output_file_path, 'w') as open_file:
            open_file.write(properties)
        return output_file_path

    def insert_browsable_files(self):
        with self.metadata_connection_handle as conn:
            # insert into browsable file table, if files not already there
            files_query = (f"select file_id, ena_submission_file_id,filename,project_accession,assembly_set_id "
                           f"from evapro.browsable_file "
                           f"where project_accession = '{self.project_accession}';")
            rows_in_table = get_all_results_for_query(conn, files_query)
            find_browsable_files_query = (
                "select file.file_id,ena_submission_file_id,filename,project_accession,assembly_set_id "
                "from (select * from analysis_file af "
                "join analysis a on a.analysis_accession = af.analysis_accession "
                "join project_analysis pa on af.analysis_accession = pa.analysis_accession "
                f"where pa.project_accession = '{self.project_accession}' ) myfiles "
                "join file on file.file_id = myfiles.file_id where file.file_type ilike 'vcf';"
            )
            rows_expected = get_all_results_for_query(conn, files_query)
            if len(rows_in_table) > 0:
                if set(rows_in_table) == set(rows_expected):
                    self.info('Browsable files already inserted, skipping')
                else:
                    self.warning(f'Found {len(rows_in_table)} browsable file rows in the table but they are different '
                                 f'from the expected ones: '
                                 f'{os.linesep + os.linesep.join([str(row) for row in rows_expected])}')
            else:
                self.info('Inserting browsable files...')
                insert_query = ("insert into browsable_file (file_id,ena_submission_file_id,filename,project_accession,"
                                "assembly_set_id) " + find_browsable_files_query)
                execute_query(conn, insert_query)

    def update_browsable_files_with_date(self):
        with self.metadata_connection_handle as conn:
            # update loaded and release date
            release_date = self.eload_cfg.query('brokering', 'ena', 'hold_date')
            release_update = f"update evapro.browsable_file " \
                             f"set loaded = true, eva_release = '{release_date.strftime('%Y%m%d')}' " \
                             f"where project_accession = '{self.project_accession}';"
            execute_query(conn, release_update)

    def update_files_with_ftp_path(self):
        files_query = f"select file_id, filename from evapro.browsable_file " \
                      f"where project_accession = '{self.project_accession}';"
        with self.metadata_connection_handle as conn:
            # update FTP file paths
            rows = get_all_results_for_query(conn, files_query)
            if len(rows) == 0:
                raise ValueError('Something went wrong with loading from ENA')
            for file_id, filename in rows:
                ftp_update = f"update evapro.file " \
                             f"set ftp_file = '/ftp.ebi.ac.uk/pub/databases/eva/{self.project_accession}/{filename}' " \
                             f"where file_id = '{file_id}';"
                execute_query(conn, ftp_update)

    def update_loaded_assembly_in_browsable_files(self):
        # find assembly associated with each browseable file and copy it to the browsable file table
        query = ('select bf.file_id, a.vcf_reference_accession '
                 'from analysis a '
                 'join analysis_file af on a.analysis_accession=af.analysis_accession '
                 'join browsable_file bf on af.file_id=bf.file_id '
                 f"where bf.project_accession='{self.project_accession}';")
        with self.metadata_connection_handle as conn:
            rows = get_all_results_for_query(conn, query)
            if len(rows) == 0:
                raise ValueError('Something went wrong with loading from ENA')

            # Update each file with its associated assembly accession
            for file_id, assembly_accession in rows:
                ftp_update = f"update evapro.browsable_file " \
                             f"set loaded_assembly = '{assembly_accession}' " \
                             f"where file_id = '{file_id}';"
                execute_query(conn, ftp_update)

    def check_assembly_set_id_coherence(self):
        query = (
            f'select a.analysis_accession, a.assembly_set_id, af.file_id, bf.assembly_set_id '
            f'from project_analysis pa '
            f'join analysis a on pa.analysis_accession=a.analysis_accession '
            f'join analysis_file af on af.analysis_accession=a.analysis_accession '
            f'join browsable_file bf on af.file_id=bf.file_id '
            f"where pa.project_accession='{self.project_accession}';"
        )
        with self.metadata_connection_handle as conn:
            for analysis_accession, assembly_set_id, file_id, assembly_set_id_from_browsable in get_all_results_for_query(conn, query):
                if assembly_set_id != assembly_set_id_from_browsable:
                    self.error(f'assembly_set_id {assembly_set_id} from analysis table is different from '
                               f'assembly_set_id {assembly_set_id} from browsable_file')

    def update_assembly_set_in_analysis(self):
        analyses = self.eload_cfg.query('submission', 'analyses')
        with self.metadata_connection_handle as conn:
            for analysis_alias, analysis_data in analyses.items():
                assembly_accession = analysis_data['assembly_accession']
                assembly_set_id = get_assembly_set_from_metadata(conn, self.taxonomy, assembly_accession)
                analysis_accession = self.eload_cfg.query('brokering', 'ena', 'ANALYSIS', analysis_alias)
                # Check if the update is needed
                check_query = (f"select assembly_set_id from evapro.analysis "
                               f"where analysis_accession = '{analysis_accession}';")
                res = get_all_results_for_query(conn, check_query)
                if res and res[0][0] != assembly_set_id:
                    if res[0][0]:
                        self.error(f'Previous assembly_set_id {res[0][0]} for {analysis_accession} was wrong and '
                                   f'will be updated to {assembly_set_id}')
                    analysis_update = (f"update evapro.analysis "
                                       f"set assembly_set_id = '{assembly_set_id}' "
                                       f"where analysis_accession = '{analysis_accession}';")
                    execute_query(conn, analysis_update)

    def refresh_study_browser(self):
        with self.metadata_connection_handle as conn:
            execute_query(conn, 'refresh materialized view study_browser;')

    @cached_property
    def valid_vcf_filenames(self):
        return list(self.project_dir.joinpath(project_dirs['valid']).glob('*.vcf.gz'))

    def run_nextflow(self, workflow_name, params, resume):
        """
        Runs a Nextflow workflow using the provided parameters.
        This will create a Nextflow work directory and delete it if the process completes successfully.
        If the process fails, the work directory is preserved and the process can be resumed.
        """
        work_dir = None
        if resume:
            work_dir = self.eload_cfg.query(self.config_section, workflow_name, 'nextflow_dir')
            if work_dir == self.nextflow_complete_value:
                self.info(f'Nextflow {workflow_name} pipeline already completed, skipping.')
                return
            if not work_dir or not os.path.exists(work_dir):
                self.warning(f'Work directory for {workflow_name} not found, will start from scratch.')
                work_dir = None
        if not resume or not work_dir:
            work_dir = self.create_nextflow_temp_output_directory(base=self.project_dir)
            self.eload_cfg.set(self.config_section, workflow_name, 'nextflow_dir', value=work_dir)

        params_file = os.path.join(self.project_dir, f'{workflow_name}_params.yaml')
        with open(params_file, 'w') as open_file:
            yaml.safe_dump(params, open_file)
        nextflow_script = os.path.join(NEXTFLOW_DIR, f'{workflow_name}.nf')

        try:
            command_utils.run_command_with_output(
                f'Nextflow {workflow_name} process',
                ' '.join((
                    'export NXF_OPTS="-Xms1g -Xmx8g"; ',
                    cfg['executable']['nextflow'], nextflow_script,
                    '-params-file', params_file,
                    '-work-dir', work_dir,
                    '-resume' if resume else ''
                ))
            )
            shutil.rmtree(work_dir)
            self.eload_cfg.set(self.config_section, str(workflow_name), 'nextflow_dir',
                               value=self.nextflow_complete_value)
        except subprocess.CalledProcessError as e:
            error_msg = f'Nextflow {workflow_name} pipeline failed: results might not be complete.'
            error_msg += (f"See Nextflow logs in {self.eload_dir}/.nextflow.log or pipeline logs "
                          f"in {self.project_dir.joinpath(project_dirs['logs'])} for more details.")
            self.error(error_msg)
            raise e
