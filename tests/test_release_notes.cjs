// Run with: node tests/test_release_notes.cjs <path-to-playwright> [live-release-json]
const fs = require('node:fs'), http = require('node:http'), path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2] || 'playwright');
const root = path.resolve(__dirname, '..');
const live = process.argv[3] ? JSON.parse(fs.readFileSync(process.argv[3], 'utf8')) : null;
const fixture = '```markdown\n### 更新说明\n- **新增** 成长计划\n- 修复问题\n\n| 平台 | 文件 |\n| --- | --- |\n| 安卓 | APK |\n\n> 提示\n\n```js\nconst x = 1;\n```\n\n[说明](https://github.com/xiaofan6ya/workbuddy2api/releases)\n\n- [x] 已完成\n```';
const server = http.createServer((req,res) => {
  const name = req.url.split('?')[0];
  const file = path.join(root, name === '/admin' ? 'admin/static/index.html' : 'admin' + name);
  if (!file.startsWith(path.join(root,'admin')) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {res.writeHead(404).end();return;}
  res.setHeader('Content-Type', {'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css'}[path.extname(file)] || 'application/octet-stream');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true, executablePath:process.env.EDGE_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors = [];
      page.on('pageerror',e=>errors.push(e.message));
      let count=0;
      await page.route('**/api/**',route => {
        const pathname=new URL(route.request().url()).pathname;
        let json={items:[],total:0};
        if(pathname==='/api/accounts') json={items:[],total:0,summary:{credits_expiring_soon:0}};
        if(pathname==='/api/app/version') json={version:'1.0.0'};
        if(pathname==='/api/app/releases') {count++;json={version:'1.0.0',platform:'android',available:true,latest:{tag:'1.0.1',asset:{browser_download_url:'https://github.com/xiaofan6ya/workbuddy2api/releases/download/1.0.1/WorkBuddy2API-1.0.1-android-arm64.apk'},installable:true},releases:[{name:'更新 1.0.1',tag:'1.0.1',body:fixture,url:'https://github.com/xiaofan6ya/workbuddy2api/releases/tag/1.0.1'}]};}
        return route.fulfill({json});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/admin`);
      await page.evaluate(()=>{window.WBMobile={};WBDesktop.checkUpdatesAtStartup();});
      await page.waitForSelector('.version-button.has-update',{state:'attached'});
      await page.evaluate(()=>WBDesktop.openUpdates());
      const notes=page.locator('.wb-release-markdown');
      assert.equal(await notes.locator('h3').textContent(),'更新说明');
      assert.equal(await notes.locator('table tbody tr').count(),1);
      assert.equal(await notes.locator('pre code').textContent(),'const x = 1;\n');
      assert.equal(await notes.locator('strong').textContent(),'新增');
      assert.equal(await notes.locator('blockquote').count(),1);
      assert.equal(await notes.locator('input[disabled]').count(),1);
      assert.match(await page.getByRole('link',{name:'下载新版 APK'}).getAttribute('href'),/\.apk$/);
      assert.equal(await page.getByRole('button',{name:'下载更新',exact:true}).count(),0);
      assert.ok(await page.locator('.wb-update-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
      await page.locator('.wb-update-dialog').screenshot({path:path.join(root,`build/release-notes-${width}.png`)});
      const safety=await page.evaluate(()=>{
        const el=document.createElement('div');
        WBReleaseNotes.render(el,'<script>window.__xss=1</script><img src=x onerror="window.__xss=2"><a href="javascript:alert(1)">bad</a><svg onload=alert(1)></svg>\n\n[bad](data:text/html,bad)\n\n[good](https://example.com)');
        return {script:el.querySelectorAll('script,img,svg').length,bad:el.querySelectorAll('[onerror],[onload],[href^="javascript:"],[href^="data:"]').length,links:[...el.querySelectorAll('a[href]')].map(a=>({href:a.href,rel:a.rel})),xss:window.__xss||0};
      });
      assert.equal(safety.script,0);assert.equal(safety.bad,0);assert.equal(safety.xss,0);assert.equal(safety.links.length,1);assert.equal(safety.links[0].rel,'noopener noreferrer');
      if(live) {
        const result=await page.evaluate(body=>{
          const el=document.querySelector('.wb-release-markdown');WBReleaseNotes.render(el,body);
          return {headings:el.querySelectorAll('h3').length,lists:el.querySelectorAll('li').length,pre:el.querySelectorAll('pre').length,text:el.textContent};
        },live.body);
        assert.ok(result.headings>=3);assert.ok(result.lists>=3);assert.equal(result.pre,0);assert.ok(result.text.includes('PyWebView'));
      }
      assert.deepEqual(errors,[]);
      assert.ok(count>=2);
      await page.evaluate(async () => {
        window.WBMobile = undefined;
        window.__updates = {downloads:0,installs:0};
        window.pywebview = {api:{
          desktop_login:async()=>({token:'test-token'}),
          check_updates:async()=>({version:'1.0.0',platform:'windows',available:true,
            latest:{id:2,tag:'1.0.1',installable:true,asset:{name:'WorkBuddy2API-1.0.1-windows-x64.exe'}},
            releases:[]}),
          download_update:async()=>{window.__updates.downloads++;return {};},
          update_status:async()=>({message:'已校验',runtime:'已准备',ready:true}),
          apply_update:async()=>{window.__updates.installs++;return {};}
        }};
        window.dispatchEvent(new Event('pywebviewready'));
      });
      await page.getByRole('button',{name:'下载更新',exact:true}).waitFor();
      assert.equal(await page.getByRole('link',{name:'下载新版 APK'}).count(),0);
      assert.equal(await page.evaluate(()=>window.__updates.downloads),0);
      assert.equal(await page.evaluate(()=>window.__updates.installs),0);
      await page.getByRole('button',{name:'下载更新',exact:true}).click();
      await page.getByRole('button',{name:'重启并安装更新',exact:true}).waitFor();
      assert.equal(await page.evaluate(()=>window.__updates.downloads),1);
      assert.equal(await page.evaluate(()=>window.__updates.installs),0);
      await page.getByRole('button',{name:'重启并安装更新',exact:true}).click();
      assert.equal(await page.evaluate(()=>window.__updates.installs),1);
      await page.getByRole('button',{name:'关闭',exact:true}).click();
      assert.deepEqual(errors,[]);
      await page.close();
      console.log(`PASS: release modal, GFM, XSS, APK link, live notice (${width}px)`);
    }
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
