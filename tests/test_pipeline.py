import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from query_external_data import _date_str, _parse_coord, _parse_coord_extent, campaign_date_window, temporal_scope


class PublicationTests(unittest.TestCase):
    def test_null_record_times_parse_as_nat(self):
        from build_fused_outputs import _parse_record_dates

        dates = _parse_record_dates(pd.DataFrame({"time": [None, float("nan")]}))
        self.assertTrue(all(pd.isna(dates)))

    def test_feature_chunks_respect_byte_budget(self):
        from build_fused_outputs import _feature_chunks

        features = [
            {"type": "Feature", "geometry": None, "properties": {"name": "x" * 80, "id": index}}
            for index in range(6)
        ]
        with patch("build_fused_outputs.MAP_CHUNK_BYTES", 300):
            chunks = list(_feature_chunks(features))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([feature for chunk in chunks for feature in chunk], features)
        self.assertTrue(all(len(json.dumps({"type": "FeatureCollection", "features": chunk}).encode()) < 400
                            for chunk in chunks))

    def test_unknown_record_depth_is_an_unverified_candidate(self):
        from build_fused_outputs import _best_matches

        samples = pd.DataFrame([{
            "campaign_id": "campaign", "sample_id": "sample", "spatial_kind": "point",
            "latitude": 42.0, "longitude": -6.0, "collection_date": "2025-01-02", "depth_m": 12.0,
        }])
        records = pd.DataFrame([{
            "record_id": "record", "lat": 42.0, "lon": -6.0,
            "time": "2025-01-02T12:00:00Z", "depth_m": None,
        }])
        matches = _best_matches(samples, records, "biology_obis")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["depth_status"], "unverified")
        self.assertIsNone(matches[0]["depth_gap_m"])


class CoordinateTests(unittest.TestCase):
    def test_signed_range(self):
        self.assertEqual(_parse_coord_extent('-6.5 - -5.0'), (-6.5, -5.0))

    def test_dms_range(self):
        low, high = _parse_coord_extent('40 29.163 N - 40 30.567 N')
        self.assertAlmostEqual(low, 40 + 29.163 / 60)
        self.assertAlmostEqual(high, 40 + 30.567 / 60)

    def test_compact_range(self):
        low, high = _parse_coord_extent('39 53.652 N-39 52.282 N')
        self.assertAlmostEqual(low, 39 + 52.282 / 60)
        self.assertAlmostEqual(high, 39 + 53.652 / 60)

    def test_range_is_not_a_point(self):
        self.assertIsNone(_parse_coord('42.0 - 45.0'))

    def test_invalid_values(self):
        for value in [math.inf, '39 23.465 N- XXXXX N', '40 61 N', 'garbage 42', '07\u00b012,544']:
            with self.subTest(value=value):
                self.assertIsNone(_parse_coord_extent(value))

    def test_existing_formats(self):
        self.assertAlmostEqual(_parse_coord('41 23 .670 N'), 41 + 23.670 / 60)
        self.assertAlmostEqual(_parse_coord('002 50,211 E'), 2 + 50.211 / 60)
        self.assertAlmostEqual(_parse_coord('7 12 44.278 W'), -(7 + 12 / 60 + 44.278 / 3600))
        self.assertEqual(_parse_coord('-6.5'), -6.5)

    def test_ambiguous_text_dates_are_not_guessed(self):
        self.assertIsNone(_date_str('03/08/2026'))
        self.assertEqual(_date_str('2026-08-03'), '2026-08-03')
        self.assertEqual(_date_str(pd.Timestamp('2026-08-03 12:30:00')), '2026-08-03')


class SampleTests(unittest.TestCase):
    def write_workbook(self, folder, filename='campaign.xlsx'):
        with pd.ExcelWriter(Path(folder) / filename) as writer:
            pd.DataFrame({'Campaign Code': ['duplicate'], 'Area of Study': ['test']}).to_excel(
                writer, sheet_name='Campaign', index=False)
            pd.DataFrame({
                'Sample Code': ['point', 'range', 'invalid'],
                'Latitude': [42, '42 - 43', None],
                'Longitude': [-6, '-6.5 - -5.0', None],
                'Date of sampling start': [pd.Timestamp('2025-01-02')] * 3,
                'Sampling duration - days': [2, 2, None],
            }).to_excel(writer, sheet_name='Sample', index=False)

    def test_retains_intervals_and_invalid_rows(self):
        from build_samples_csv import read_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder)
            frame, audit = read_samples(folder, duration_mode='elapsed')
            self.assertEqual(len(frame), 3)
            self.assertEqual(frame['spatial_kind'].tolist(), ['point', 'extent', 'missing'])
            self.assertEqual(frame.loc[1, 'latitude_max'], 43)
            self.assertTrue(pd.isna(frame.loc[1, 'longitude']))
            self.assertEqual(frame.loc[0, 'collection_end'], '2025-01-04')
            self.assertEqual(frame.loc[0, 'source_row'], 2)
            self.assertEqual(audit[0]['sample_rows'], 3)

    def test_duration_requires_explicit_interpretation(self):
        from build_samples_csv import read_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder)
            frame, _ = read_samples(folder, duration_mode=None)
            self.assertTrue(pd.isna(frame.loc[0, 'collection_end']))
            self.assertIn('duration_semantics_unconfirmed', frame.loc[0, 'quality_flags'])
            inclusive, _ = read_samples(folder, duration_mode='inclusive')
            self.assertEqual(inclusive.loc[0, 'collection_end'], '2025-01-03')

    def test_duplicate_campaign_codes_do_not_merge(self):
        from build_samples_csv import read_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder, 'first.xlsx')
            initial, _ = read_samples(folder)
            self.write_workbook(folder, 'second.xlsx')
            frame, audit = read_samples(folder)
            self.assertEqual(frame['campaign_id'].nunique(), 2)
            self.assertTrue(frame['sample_id'].is_unique)
            self.assertEqual(initial['sample_id'].tolist(), frame.iloc[:3]['sample_id'].tolist())
            self.assertTrue(all(item['duplicate_campaign_code'] for item in audit))

    def test_campaign_roundtrip_and_input_fingerprint(self):
        from build_samples_csv import build_samples_csv
        from query_external_data import _campaigns_from_csv, _campaigns_from_frame
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder)
            output = Path(folder) / 'samples.csv'
            frame = build_samples_csv(folder, output, duration_mode='elapsed')
            expected = _campaigns_from_frame(frame)
            self.assertEqual(expected, _campaigns_from_csv(folder, output))
            self.assertEqual(expected[0]['lon_min'], -6.5)
            self.assertEqual(expected[0]['lat_max'], 43)
            self.assertEqual(expected[0]['date_max'], '2025-01-04')
            self.write_workbook(folder, 'another.xlsx')
            self.assertIsNone(_campaigns_from_csv(folder, output))

    def test_unreviewed_duration_blocks_fetching(self):
        from build_samples_csv import build_samples_csv
        from query_external_data import load_campaigns_from_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder)
            output = Path(folder) / 'samples.csv'
            build_samples_csv(folder, output, duration_mode=None)
            with self.assertRaisesRegex(ValueError, 'review required'):
                load_campaigns_from_samples(folder, output)
            self.assertEqual(len(load_campaigns_from_samples(folder, output, allow_unresolved=True)), 1)

    def test_point_bounds_and_text_identifiers(self):
        from build_samples_csv import build_samples_csv
        from query_external_data import load_campaigns_from_samples
        with tempfile.TemporaryDirectory() as folder:
            with pd.ExcelWriter(Path(folder) / 'single.xlsx') as writer:
                pd.DataFrame({'Campaign Code': ['001'], 'Area of Study': ['NA']}).to_excel(
                    writer, sheet_name='Campaign', index=False)
                pd.DataFrame({'Sample Code': ['0001'], 'Latitude': [42], 'Longitude': [-6],
                              'Date of sampling start': [pd.Timestamp('2025-01-02')]}).to_excel(
                    writer, sheet_name='Sample', index=False)
            output = Path(folder) / 'samples.csv'
            frame = build_samples_csv(folder, output)
            self.assertEqual(frame.loc[0, 'sample_code'], '0001')
            campaign = load_campaigns_from_samples(folder, output)[0]
            self.assertEqual(campaign['campaign_code'], '001')
            self.assertEqual(campaign['area'], 'NA')
            self.assertEqual((campaign['lat_min'], campaign['lat_max']), (42, 42))
            self.assertEqual((campaign['lon_min'], campaign['lon_max']), (-6, -6))

    def test_unreadable_workbook_is_reported_and_blocks_fetch(self):
        from build_samples_csv import read_samples, build_samples_csv
        from query_external_data import load_campaigns_from_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder)
            (Path(folder) / 'broken.xlsx').write_bytes(b'not an Excel workbook')
            _, audit = read_samples(folder)
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[0]['status'], 'error')
            output = Path(folder) / 'samples.csv'
            build_samples_csv(folder, output)
            with self.assertRaisesRegex(ValueError, 'Unreadable workbook'):
                load_campaigns_from_samples(folder, output, allow_unresolved=True)

    def test_inclusive_default_and_ignored_hours(self):
        from build_samples_csv import build_samples_csv
        from query_external_data import load_campaigns_from_samples
        with tempfile.TemporaryDirectory() as folder:
            with pd.ExcelWriter(Path(folder) / 'days.xlsx') as writer:
                pd.DataFrame({
                    'Sample Code': ['number', 'clock', 'range'],
                    'Latitude': [42] * 3, 'Longitude': [15.727] * 3,
                    'Date of sampling start': [pd.Timestamp('2026-08-03')] * 3,
                    'Sampling duration - days': [1] * 3,
                    'Sampling duration - hours': [2, '12:30:00', '23:30 - 00:30'],
                }).to_excel(writer, sheet_name='Sample', index=False)
            output = Path(folder) / 'samples.csv'
            frame = build_samples_csv(folder, output)
            self.assertEqual(frame.collection_end.tolist(), ['2026-08-03'] * 3)
            self.assertEqual(frame.duration_hours_raw.tolist(), ['2', '12:30:00', '23:30 - 00:30'])
            self.assertTrue(frame.quality_flags.str.contains('hours_ignored_day_resolution').all())
            campaign = load_campaigns_from_samples(folder, output)[0]
            self.assertEqual(campaign['date_min'], '2026-08-03')
            self.assertEqual(campaign['date_max'], '2026-08-03')
            self.assertEqual(campaign['query_date_min'], '2026-07-04')
            self.assertEqual(campaign['query_date_max'], '2026-09-02')
            self.assertEqual(campaign['lon_max'], 15.727)
            self.assertEqual(campaign['blocking_reasons'], [])

    def test_retained_warnings_do_not_block_queries(self):
        from build_samples_csv import build_samples_csv
        from query_external_data import load_campaigns_from_samples
        with tempfile.TemporaryDirectory() as folder:
            self.write_workbook(folder, 'first.xlsx')
            self.write_workbook(folder, 'second.xlsx')
            output = Path(folder) / 'samples.csv'
            build_samples_csv(folder, output)
            campaigns = load_campaigns_from_samples(folder, output)
            self.assertEqual(len(campaigns), 2)
            self.assertTrue(all('duplicate_campaign_metadata' in camp['review_reasons'] for camp in campaigns))
            self.assertTrue(all(not camp['blocking_reasons'] for camp in campaigns))


class DateWindowTests(unittest.TestCase):
    def test_temporal_scope_keeps_uncertain_intervals_distinct(self):
        campaign = {'date_min': '2026-08-03', 'date_max': '2026-08-03'}
        self.assertEqual(temporal_scope(campaign, '2026-08-03T12:00:00Z'), 'within_sample_window')
        self.assertEqual(temporal_scope(campaign, '2026-08-02'), 'context')
        self.assertEqual(temporal_scope(campaign, '2026-08-02/2026-08-04'), 'overlaps_sample_window')
        self.assertEqual(temporal_scope(campaign, '2026'), 'unknown')
        self.assertEqual(temporal_scope(campaign, None), 'unknown')

    def test_padding_crosses_calendar_boundaries_without_mutation(self):
        campaign = {'date_min': '2024-03-01', 'date_max': '2024-03-01'}
        expected = ('2024-01-31', '2024-03-31')
        self.assertEqual(campaign_date_window(campaign), expected)
        self.assertEqual(campaign_date_window(campaign), expected)
        self.assertEqual(campaign['date_min'], '2024-03-01')
        self.assertEqual(campaign_date_window(campaign, 0), ('2024-03-01', '2024-03-01'))

    def test_invalid_or_unbounded_windows_are_rejected(self):
        for campaign in [{}, {'date_min': '2024-03-02', 'date_max': '2024-03-01'}]:
            with self.assertRaises(ValueError):
                campaign_date_window(campaign)
        for padding in [-1, 0.5, True]:
            with self.assertRaises(ValueError):
                campaign_date_window({'date_min': '2024-03-01', 'date_max': '2024-03-01'}, padding)


class ProviderDateTests(unittest.TestCase):
    def setUp(self):
        self.campaign = {
            'name': 'sample', 'area': 'test', 'date_min': '2026-08-03', 'date_max': '2026-08-03',
            'lat_min': 41, 'lat_max': 43, 'lon_min': -7, 'lon_max': -5, 'clat': 42, 'clon': -6,
        }

    def test_gbif_uses_padded_dates_and_preserves_sample_window(self):
        import fetch_gbif_occurrences as gbif
        response = SimpleNamespace(status_code=200, url='https://example.test/gbif', json=lambda: {
            'results': [{'key': 1, 'kingdom': 'Animalia', 'decimalLatitude': 42,
                         'decimalLongitude': -6, 'eventDate': '2026-07-04'}], 'endOfRecords': True,
        })
        with patch.object(gbif.requests, 'get', return_value=response) as request:
            rows = gbif.fetch_one(self.campaign)
        self.assertEqual(request.call_args.kwargs['params']['eventDate'], '2026-07-04,2026-09-02')
        self.assertEqual(rows[0]['campaign_date_min'], '2026-08-03')
        self.assertEqual(rows[0]['query_date_min'], '2026-07-04')
        self.assertEqual(rows[0]['temporal_scope'], 'context')

    def test_climate_uses_padded_dates_and_labels_each_day(self):
        import fetch_climate as climate
        hourly = {name: [1.0, 1.0] for name in climate.HOURLY_VARS}
        hourly['time'] = ['2026-07-04T00:00', '2026-08-03T00:00']
        response = SimpleNamespace(status_code=200, url='https://example.test/climate', json=lambda: {'hourly': hourly})
        with patch.object(climate.requests, 'get', return_value=response) as request:
            rows = climate.fetch_one(self.campaign)
        self.assertEqual(request.call_args.kwargs['params']['start_date'], '2026-07-04')
        self.assertEqual(request.call_args.kwargs['params']['end_date'], '2026-09-02')
        self.assertEqual([row['temporal_scope'] for row in rows], ['context', 'within_sample_window'])
        self.assertTrue(all(row['campaign_date_max'] == '2026-08-03' for row in rows))

    def test_obis_uses_padded_dates(self):
        import build_campaign_datasources as main
        response = SimpleNamespace(status_code=200, json=lambda: {'results': []})
        with patch.object(main.requests, 'get', return_value=response) as request, patch.object(main.time, 'sleep'):
            main.fetch_biology([self.campaign])
        self.assertEqual(request.call_args.kwargs['params']['startdate'], '2026-07-04')
        self.assertEqual(request.call_args.kwargs['params']['enddate'], '2026-09-02')

    def test_obis_tiles_capped_results_and_deduplicates_boundaries(self):
        import build_campaign_datasources as main
        calls = []
        def response(url, params, timeout):
            calls.append(params)
            if len(calls) == 1:
                payload = {'total': 5001, 'results': [{'id': 'ignored-root'}]}
            elif len(calls) == 2:
                payload = {'total': 2, 'results': [
                    {'id': 'left', 'decimalLatitude': 42, 'decimalLongitude': -6},
                    {'id': 'boundary', 'decimalLatitude': 42, 'decimalLongitude': -6},
                ]}
            else:
                payload = {'total': 2, 'results': [
                    {'id': 'boundary', 'decimalLatitude': 42, 'decimalLongitude': -6},
                    {'id': 'right', 'decimalLatitude': 42, 'decimalLongitude': -5},
                ]}
            return SimpleNamespace(status_code=200, url='https://example.test/obis', json=lambda: payload)
        with patch.object(main.requests, 'get', side_effect=response), patch.object(main.time, 'sleep'):
            rows = main.fetch_biology([self.campaign])
        self.assertEqual(len(calls), 3)
        self.assertEqual({row['feature_id'] for row in rows}, {'left', 'boundary', 'right'})
        self.assertTrue(all(call['startdate'] == '2026-07-04' for call in calls))

    def test_argo_uses_whole_padded_final_day(self):
        import build_campaign_datasources as main
        fetcher = SimpleNamespace(region=Mock(side_effect=FileNotFoundError))
        module = SimpleNamespace(DataFetcher=lambda **kwargs: fetcher)
        with patch.dict('sys.modules', {'argopy': module}):
            main.fetch_argo([self.campaign])
        self.assertEqual(fetcher.region.call_args.args[0][-2:],
                         ['2026-07-04T00:00:00Z', '2026-09-02T23:59:59Z'])

    def test_emso_uses_padded_dates(self):
        import build_campaign_datasources as main
        node = {'lat': 42, 'lon': -6, 'node': 'test', 'dataset_ids': ['dataset']}
        with patch.object(main, 'EMSO_NODES', [node]), \
             patch.object(main, '_emso_discover_vars', return_value=['time']), \
             patch.object(main, 'safe_get', return_value=None) as request, patch.object(main.time, 'sleep'):
            main.fetch_emso([self.campaign])
        url = request.call_args.args[0]
        self.assertIn('&time%3E=2026-07-04T00:00:00Z', url)
        self.assertIn('&time%3C=2026-09-02T23:59:59Z', url)

    def test_common_row_preserves_actual_window(self):
        import build_campaign_datasources as main
        row = main._row(self.campaign, time='2026-07-04T00:00:00Z', data_type='observation')
        self.assertEqual(row['campaign_date_min'], '2026-08-03')
        self.assertEqual(row['query_date_min'], '2026-07-04')
        self.assertEqual(row['temporal_scope'], 'context')

    def test_chemistry_sends_padded_time_filter(self):
        import build_campaign_datasources as main
        with patch.object(main, '_basins_for_campaign', return_value=['test']), \
             patch.object(main, 'EMODCHEM_BASINS', {'test': ['dataset']}), \
             patch.object(main, '_emodchem_vars', return_value=['time', 'latitude', 'longitude']), \
             patch.object(main.requests, 'get') as request, patch.object(main.time, 'sleep'):
            request.return_value.__enter__.return_value.status_code = 404
            main.fetch_emodnet_chemistry([self.campaign])
        url = request.call_args.args[0]
        self.assertIn('&time%3E=2026-07-04T00:00:00Z', url)
        self.assertIn('&time%3C=2026-09-02T23:59:59Z', url)

    def test_copernicus_manifest_records_padded_window_not_fake_observation(self):
        import json
        import build_campaign_datasources as main
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict('sys.modules', {'copernicusmarine': None}), \
             patch.object(main, 'OUT_DIR', folder), \
             patch.object(main, 'CMEMS_PRODUCTS', [('dataset', ['temperature'], 0)]):
            rows = main.fetch_copernicus([self.campaign])
        parameters = json.loads(rows[0]['extra_json'])
        self.assertEqual(parameters['start_datetime'], '2026-07-04T00:00:00Z')
        self.assertEqual(parameters['end_datetime'], '2026-09-02T23:59:59Z')
        self.assertEqual(rows[0]['time'], '')
        self.assertEqual(rows[0]['geom_wkt'], '')
        self.assertEqual(rows[0]['temporal_scope'], 'unknown')


if __name__ == '__main__':
    unittest.main()