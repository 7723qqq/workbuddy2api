"""Launcher recovery and fragmented SSE used by Android settings."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('android_runtime', ROOT / 'android/python/android_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class AndroidSettingsTests(unittest.TestCase):
    def test_model_rows_render_without_object_hasown_on_old_webview(self):
        if not shutil.which('node'):
            self.skipTest('Node required for browser compatibility regression')
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');
const labels=html.slice(html.indexOf('const MODEL_BUSY_LABELS'),html.indexOf('function scheduleModelBusy'));
const context={esc:x=>String(x),fmtTime:x=>String(x),Object};
const previous=Object.hasOwn;Object.hasOwn=undefined;
try {
 vm.runInNewContext(labels + `
 if(!modelBusyBadge({model_id:'hy3'}).includes('未知'))throw Error('Missing model badge');
 _modelBusy.hy3={busy_level:'idle'};
 if(!modelBusyBadge({model_id:'hy3'}).includes('空闲'))throw Error('Known model badge missing');`,context);
} finally {Object.hasOwn=previous;}
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'admin/static/index.html')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repairs_launcher_config_without_removing_database(self):
        for content in ('{broken', '[]', '{"port":null,"password":null}',
                        '{"port":"invalid","username":"","jwt_secret":123}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.json'
                database = Path(directory) / 'workbuddy.db'
                database.write_bytes(b'preserve-existing-account-data')
                path.write_text(content, encoding='utf-8')
                config = runtime.load_config(path)
                self.assertEqual(config['port'], 8790)
                for key in ('username', 'password', 'jwt_secret'):
                    self.assertIsInstance(config[key], str)
                    self.assertTrue(config[key])
                self.assertEqual(database.read_bytes(), b'preserve-existing-account-data')
                self.assertEqual(runtime.load_config(path), config)
                if content in ('{broken', '[]'):
                    self.assertEqual(len(list(Path(directory).glob('config.invalid-*.json'))), 1)

    def test_fragmented_sse_ignores_heartbeat_and_requires_real_text(self):
        if not shutil.which('node'):
            self.skipTest('Node required to run browser stream parser')
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8').split('window.WBMobileSettings =')[0];
const context = {};
vm.runInNewContext(source + ';this.Meter = MobileStreamMeter;', context);
const meter = new context.Meter(100);
meter.feed(': heartbeat\r\n\r\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\r\n\r\n', 200);
assert.equal(meter.firstText, null);
meter.feed('data: {"choices":[{"delta":{"reasoning_con', 350);
meter.feed('tent":"思考"}}]}\r\n\r\ndata: {"choices":[{"delta":{"content":"你好"}}]}\n\n', 400);
assert.equal(meter.firstText, 300);
meter.feed('data: {"usage":{"completion_tokens":12}}\n\ndata: [DO', 450);
assert.equal(meter.done, false);
meter.feed('NE]\n\n', 500);
assert.equal(meter.done, true); assert.equal(meter.tokens, 12); assert.equal(meter.sample, '思考你好');
for (const [delta, terminal] of [
 [{type:'response.output_text.delta', delta:'答案'}, {type:'response.completed', response:{usage:{output_tokens:5}}}],
 [{type:'content_block_delta', delta:{type:'text_delta', text:'答案'}}, {type:'message_stop'}],
]) {
 const item = new context.Meter(0);
 item.feed('data: '+JSON.stringify(delta)+'\n\n', 17);
 item.feed('data: '+JSON.stringify(terminal), 20); item.end(21);
 assert.equal(item.firstText, 17); assert.equal(item.done, true); assert.equal(item.sample, '答案');
}
const error = new context.Meter(0);
error.feed('data: {"type":"response.incomplete"}\n\n', 9);
assert.ok(error.error); assert.equal(error.done, false);
const broken = new context.Meter(0); broken.feed('data: not-json\n\n', 9); assert.ok(broken.error);
'''
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(ROOT / 'android/web/mobile_settings.js')],
                                capture_output=True, text=True, encoding='utf-8', timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
