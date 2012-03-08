#!/usr/bin/env python

from datetime import date, time, datetime
import logging
from math import floor

from django.conf import settings
import xlrd

from panda import solr, utils
from panda.exceptions import TypeCoercionError
from panda.tasks.import_file import ImportFileTask
from panda.tasks.reindex import TYPE_NAMES_MAPPING 
from panda.utils.typecoercion import coerce_type

SOLR_ADD_BUFFER_SIZE = 500

class ImportXLSTask(ImportFileTask):
    """
    Task to import all data for a dataset from an Excel XLS file.
    """
    name = 'panda.tasks.import.xls'

    def run(self, dataset_slug, upload_id, external_id_field_index=None, *args, **kwargs):
        """
        Execute import.
        """
        from panda.models import Dataset, DataUpload
        
        log = logging.getLogger(self.name)
        log.info('Beginning import, dataset_slug: %s' % dataset_slug)

        dataset = Dataset.objects.get(slug=dataset_slug)
        upload = DataUpload.objects.get(id=upload_id)

        task_status = dataset.current_task
        task_status.begin('Preparing to import')

        book = xlrd.open_workbook(upload.get_path(), on_demand=True)
        sheet = book.sheet_by_index(0)
        row_count = sheet.nrows
        
        add_buffer = []
        schema = dataset.column_schema

        for i in range(1, row_count):
            values = sheet.row_values(i)
            types = sheet.row_types(i)

            normal_values = []

            for v, t in zip(values, types):
                if t == xlrd.biffh.XL_CELL_DATE:
                    v = utils.xls.normalize_date(v, book.datemode)
                elif t == xlrd.biffh.XL_CELL_NUMBER:
                    if v % 1 == 0:
                        v = int(v)

                normal_values.append(unicode(v))

            external_id = None

            if external_id_field_index is not None:
                external_id = values[external_id_field_index]

            data = utils.solr.make_data_row(dataset, values, external_id=external_id)

            # Generate typed column data
            for n, c in enumerate(schema):
                if c['indexed'] and c['type']:
                    try:
                        t = TYPE_NAMES_MAPPING[c['type']]
                        value = coerce_type(values[n], t)
                        data[c['indexed_name']] = value

                        if t in [int, float, date, time, datetime]:
                            if t is date:
                                value = value.date()
                            elif t is time:
                                value = value.time()
                            
                            if c['min'] is None or value < c['min']:
                                c['min'] = value

                            if c['max'] is None or value > c['max']:
                                c['max'] = value
                    except TypeCoercionError, e:
                        # TODO: log here
                        pass

            add_buffer.append(data)

            if i % SOLR_ADD_BUFFER_SIZE == 0:
                solr.add(settings.SOLR_DATA_CORE, add_buffer)
                add_buffer = []

                task_status.update('%.0f%% complete' % floor(float(i) / float(row_count) * 100))

                if self.is_aborted():
                    task_status.abort('Aborted after importing %.0f%%' % floor(float(i) / float(row_count) * 100))

                    log.warning('Import aborted, dataset_slug: %s' % dataset_slug)

                    return

        if add_buffer:
            solr.add(settings.SOLR_DATA_CORE, add_buffer)
            add_buffer = []

        solr.commit(settings.SOLR_DATA_CORE)

        task_status.update('100% complete')

        # Refresh dataset from database so there is no chance of crushing changes made since the task started
        dataset = Dataset.objects.get(slug=dataset_slug)

        if not dataset.row_count:
            dataset.row_count = i
        else:
            dataset.row_count += i

        dataset.column_schema = schema

        dataset.save()

        # Refres
        upload = DataUpload.objects.get(id=upload_id)

        upload.imported = True
        upload.save()

        log.info('Finished import, dataset_slug: %s' % dataset_slug)

