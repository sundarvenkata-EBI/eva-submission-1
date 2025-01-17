import os
from datetime import datetime
from unittest import TestCase
import xml.etree.ElementTree as ET
from unittest.mock import patch, Mock

from eva_submission import ROOT_DIR
from eva_submission.ENA_submission.xlsx_to_ENA_xml import EnaXlsxConverter


def elements_equal(e1, e2):
    """Test if the two elements are the same based on their tag, text, attributes and children"""
    if e1.tag != e2.tag:
        print('Tag: %s != %s' % (e1.tag, e2.tag))
        return False
    if e1.text and e2.text and e1.text.strip() != e2.text.strip():
        print('Text: %s != %s' % (e1.text.strip(), e2.text.strip()))
        return False
    if e1.tail and e2.tail and e1.tail.strip() != e2.tail.strip():
        print('Tail: %s != %s' % (e1.tail.strip(), e2.tail.strip()))
        return False
    if e1.attrib != e2.attrib:
        print('Attrib: %s != %s' % (e1.attrib, e2.attrib))
        return False
    if len(e1) != len(e2):
        print('length %s (%s) != length %s (%s) ' % (e1.tag, len(e1), e2.tag, len(e2)))
        return False
    return all(elements_equal(c1, c2) for c1, c2 in zip(sorted(e1, key=lambda x: x.tag), sorted(e2, key=lambda x: x.tag)))


class TestEnaXlsConverter(TestCase):

    expected_analysis = '''
    <ANALYSIS_SET>
      <ANALYSIS alias="GRM" center_name="Laboratory of Aquatic Pathobiology">
        <TITLE>Genomic Relationship Matrix</TITLE>
        <DESCRIPTION>A genomic relationship matrix (GRM) was computed</DESCRIPTION>
        <STUDY_REF refname="TechFish"/>
        <SAMPLE_REF accession="SAMEA7851610" label="201903VIBRIO1185679118"/>
        <SAMPLE_REF accession="SAMEA7851611" label="201903VIBRIO1185679119"/>
        <SAMPLE_REF accession="SAMEA7851612" label="201903VIBRIO1185679120"/>
        <SAMPLE_REF accession="SAMEA7851613" label="201903VIBRIO1185679121"/>
        <SAMPLE_REF accession="SAMEA7851614" label="201903VIBRIO1185679122"/>
        <ANALYSIS_TYPE>
          <SEQUENCE_VARIATION>
            <ASSEMBLY>
              <STANDARD accession="GCA_002163495.1"/>
            </ASSEMBLY>
            <EXPERIMENT_TYPE>Genotyping by array</EXPERIMENT_TYPE>
            <PROGRAM>software package GCTA, Burrows-Wheeler Alignment tool (BWA), HTSeq-python package</PROGRAM>
          </SEQUENCE_VARIATION>
        </ANALYSIS_TYPE>
        <FILES>
          <FILE filename="Vibrio.chrom.fix2.final.debug.gwassnps.vcf.gz" filetype="vcf" checksum_method="MD5" checksum="c263a486e9b273d6e1e4c5f46ca5ccb8"/>
          <FILE filename="Vibrio.chrom.fix2.final.debug.gwassnps.vcf.gz.tbi" filetype="tabix" checksum_method="MD5" checksum="4b61e00524cc1f4c98e932b0ee27d94e"/>
        </FILES>
        <ANALYSIS_ATTRIBUTES/>
      </ANALYSIS>
    </ANALYSIS_SET>
    '''

    def setUp(self) -> None:
        self.brokering_folder = os.path.join(ROOT_DIR, 'tests', 'resources', 'brokering')

        self.project_row = {
            'Project Title': 'TechFish - Vibrio challenge',
            'Project Alias': 'TechFish',
            'Description': 'Identification of a major QTL for resistance to Vibrio anguillarum in rainbow trout',
            'Center': 'Laboratory of Aquatic Pathobiology',
            'Tax ID': 8022,
            'Publication(s)': 'PubMed:123456,PubMed:987654',
            'Link(s)': 'https://www.biorxiv.org/content/example/manuscript|preprint url'
        }

        self.analysis_row = {
            'Analysis Title': 'Genomic Relationship Matrix',
            'Analysis Alias': 'GRM',
            'Description': 'A genomic relationship matrix (GRM) was computed',
            'Project Title': 'TechFish - Vibrio challenge',
            'Experiment Type': 'Genotyping by array',
            'Reference': 'GCA_002163495.1',
            'Software': 'software package GCTA, Burrows-Wheeler Alignment tool (BWA), HTSeq-python package ',
        }

        self.sample_rows = [
            {'Analysis Alias': 'GRM', 'Sample ID': '201903VIBRIO1185679118', 'Sample Accession': 'SAMEA7851610'},
            {'Analysis Alias': 'GRM', 'Sample ID': '201903VIBRIO1185679119', 'Sample Accession': 'SAMEA7851611'},
            {'Analysis Alias': 'GRM', 'Sample ID': '201903VIBRIO1185679120', 'Sample Accession': 'SAMEA7851612'},
            {'Analysis Alias': 'GRM', 'Sample ID': '201903VIBRIO1185679121', 'Sample Accession': 'SAMEA7851613'},
            {'Analysis Alias': 'GRM', 'Sample ID': '201903VIBRIO1185679122', 'Sample Accession': 'SAMEA7851614'},
        ]

        self.file_rows = [
            {'Analysis Alias': 'GRM', 'File Name': 'Vibrio.chrom.fix2.final.debug.gwassnps.vcf.gz',
             'File Type': 'vcf', 'MD5': 'c263a486e9b273d6e1e4c5f46ca5ccb8'},
            {'Analysis Alias': 'GRM', 'File Name': 'Vibrio.chrom.fix2.final.debug.gwassnps.vcf.gz.tbi',
             'File Type': 'tabix', 'MD5': '4b61e00524cc1f4c98e932b0ee27d94e'},
        ]

        self.metadata_file = os.path.join(self.brokering_folder, 'metadata_sheet.xlsx')
        self.converter = EnaXlsxConverter(self.metadata_file, self.brokering_folder, 'TEST1')

    @staticmethod
    def _delete_file(file_path):
        if os.path.exists(file_path):
            os.remove(file_path)

    def tearDown(self) -> None:
        self._delete_file(os.path.join(self.brokering_folder, 'TEST1.Submission.xml'))
        self._delete_file(os.path.join(self.brokering_folder, 'TEST1.Project.xml'))
        self._delete_file(os.path.join(self.brokering_folder, 'TEST1.Analysis.xml'))

    def test_create_project(self):
        expected_project = '''
<PROJECT_SET>
  <PROJECT alias="TechFish" center_name="Laboratory of Aquatic Pathobiology">
    <TITLE>TechFish - Vibrio challenge</TITLE>
    <DESCRIPTION>Identification of a major QTL for resistance to Vibrio anguillarum in rainbow trout</DESCRIPTION>
    <PUBLICATIONS>
      <PUBLICATION>
        <PUBLICATION_LINKS>
          <PUBLICATION_LINK>
            <XREF_LINK>
              <DB>PubMed</DB>
              <ID>123456</ID>
            </XREF_LINK>
          </PUBLICATION_LINK>
        </PUBLICATION_LINKS>
      </PUBLICATION>
      <PUBLICATION>
        <PUBLICATION_LINKS>
          <PUBLICATION_LINK>
            <XREF_LINK>
              <DB>PubMed</DB>
              <ID>987654</ID>
            </XREF_LINK>
          </PUBLICATION_LINK>
        </PUBLICATION_LINKS>
      </PUBLICATION>
    </PUBLICATIONS>
    <SUBMISSION_PROJECT>
      <SEQUENCING_PROJECT/>
      <ORGANISM>
        <TAXON_ID>8022</TAXON_ID>
        <SCIENTIFIC_NAME>Oncorhynchus mykiss</SCIENTIFIC_NAME>
      </ORGANISM>
    </SUBMISSION_PROJECT>
    <PROJECT_LINKS>
        <PROJECT_LINK>
            <URL_LINK>
                <LABEL>preprint url</LABEL>
                <URL>https://www.biorxiv.org/content/example/manuscript</URL>
            </URL_LINK>
        </PROJECT_LINK>
    </PROJECT_LINKS>
    <PROJECT_ATTRIBUTES/>
  </PROJECT>
</PROJECT_SET>
'''
        self.converter.reader = Mock(project=self.project_row)
        with patch('eva_submission.ENA_submission.xlsx_to_ENA_xml.get_scientific_name_from_ensembl') as m_sci_name:
            m_sci_name.return_value = 'Oncorhynchus mykiss'
            root = self.converter._create_project_xml()
            expected_root = ET.fromstring(expected_project)
            assert elements_equal(root, expected_root)

    def test_add_analysis(self):
        root = ET.Element('ANALYSIS_SET')
        self.converter._add_analysis(root, self.analysis_row, self.project_row, self.sample_rows, self.file_rows)
        assert elements_equal(root, ET.fromstring(self.expected_analysis))

    def test_add_analysis_to_existing_project(self):
        self.project_row['Project Alias'] = 'PRJEB00001'

        root = ET.Element('ANALYSIS_SET')
        self.converter._add_analysis(root, self.analysis_row, self.project_row, self.sample_rows, self.file_rows)
        assert elements_equal(root, ET.fromstring(
            self.expected_analysis.replace('<STUDY_REF refname="TechFish"/>','<STUDY_REF accession="PRJEB00001"/>')
        ))

    def test_process_metadata_spreadsheet(self):
        with patch('eva_submission.ENA_submission.xlsx_to_ENA_xml.get_scientific_name_from_ensembl') as m_sci_name:
            m_sci_name.return_value = 'Oncorhynchus mykiss'
            self.converter.create_submission_files('TEST1')
        assert os.path.isfile(os.path.join(self.brokering_folder, 'TEST1.Submission.xml'))
        assert os.path.isfile(os.path.join(self.brokering_folder, 'TEST1.Project.xml'))
        assert os.path.isfile(os.path.join(self.brokering_folder, 'TEST1.Analysis.xml'))

    def test_create_submission(self):
        expected_submission = '''
<SUBMISSION_SET>
  <SUBMISSION alias="ELOAD_1" center_name="Laboratory of Aquatic Pathobiology">
    <ACTIONS>
      <ACTION>
        <ADD source="project.xml" schema="project"/>
      </ACTION>
      <ACTION>
        <ADD source="analysis.xml" schema="analysis"/>
      </ACTION>
      <ACTION>
        <HOLD HoldUntilDate="2021-01-04"/>
      </ACTION>
    </ACTIONS>
  </SUBMISSION>
</SUBMISSION_SET>
'''
        files_to_submit = [
            {'file_name': 'path/to/project.xml', 'schema': 'project'},
            {'file_name': 'path/to/analysis.xml', 'schema': 'analysis'}
        ]
        with patch('eva_submission.ENA_submission.xlsx_to_ENA_xml.today',
                   return_value=datetime(year=2021, month=1, day=1)):
            root = self.converter._create_submission_xml(files_to_submit, 'ADD', self.project_row, 'ELOAD_1')
            expected_root = ET.fromstring(expected_submission)
        assert elements_equal(root, expected_root)

    def test_create_submission_with_date(self):
        self.project_row['Hold Date'] = datetime(year=2023, month=6, day=25)
        expected_submission = '''
<SUBMISSION_SET>
  <SUBMISSION alias="ELOAD_1" center_name="Laboratory of Aquatic Pathobiology">
    <ACTIONS>
      <ACTION>
        <ADD source="project.xml" schema="project"/>
      </ACTION>
      <ACTION>
        <ADD source="analysis.xml" schema="analysis"/>
      </ACTION>
      <ACTION>
        <HOLD HoldUntilDate="2023-06-25"/>
      </ACTION>
    </ACTIONS>
  </SUBMISSION>
</SUBMISSION_SET>
'''
        root = ET.Element('SUBMISSION_SET')
        files_to_submit = [
            {'file_name': 'path/to/project.xml', 'schema': 'project'},
            {'file_name': 'path/to/analysis.xml', 'schema': 'analysis'}
        ]
        root = self.converter._create_submission_xml(files_to_submit, 'ADD', self.project_row, 'ELOAD_1')
        expected_root = ET.fromstring(expected_submission)
        assert elements_equal(root, expected_root)

    def test_create_submission_files(self):
        submission_file, project_file, analysis_file = self.converter.create_submission_files('ELOAD_1')
        assert os.path.exists(submission_file)
        assert os.path.exists(project_file)
        assert os.path.exists(analysis_file)

    def test_create_submission_files_for_existing_project(self):
        # When the project already exist not PROJECT XML will be generated
        with patch.object(EnaXlsxConverter, 'is_existing_project', return_value=True):
            submission_file, project_file, analysis_file = self.converter.create_submission_files('ELOAD_1')
            assert os.path.exists(submission_file)
            assert project_file is None
            assert os.path.exists(analysis_file)
            assert not os.path.exists(self.converter.project_file)

    def test_create_single_submission_files(self):
        self.converter.create_single_submission_file('ELOAD_1')


