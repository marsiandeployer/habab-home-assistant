import tempfile
import unittest
from pathlib import Path

from demo_service_worker import RETIREMENT_FILENAMES, publish_retirement_worker


class DemoServiceWorkerPublisherTests(unittest.TestCase):
    def test_publisher_installs_identical_workers_and_idempotent_headers(self):
        source = Path(__file__).with_name('retire-demo-service-worker.js')
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            (public / '_headers').write_text('/*\n  Cache-Control: max-age=0\n', encoding='utf-8')

            publish_retirement_worker(public, source)
            first_headers = (public / '_headers').read_text(encoding='utf-8')
            for filename in RETIREMENT_FILENAMES:
                self.assertEqual((public / filename).read_bytes(), source.read_bytes())
                self.assertIn(f'/{filename}\n  Cache-Control: no-cache, no-store, must-revalidate', first_headers)

            publish_retirement_worker(public, source)
            self.assertEqual((public / '_headers').read_text(encoding='utf-8'), first_headers)
            self.assertEqual(first_headers.count('# HABAB demo service-worker retirement headers:begin'), 1)


if __name__ == '__main__':
    unittest.main()
