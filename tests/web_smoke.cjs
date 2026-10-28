/* Optional real-browser acceptance. Requires Playwright and Edge/Chromium. */
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');

(async()=>{
  const root=path.resolve(__dirname,'..');
  fs.mkdirSync(path.join(root,'.test_tmp'),{recursive:true});
  const artifacts=fs.mkdtempSync(path.join(root,'.test_tmp','web-acceptance-'));
  const password=crypto.randomBytes(18).toString('base64url');
  const memberPassword=crypto.randomBytes(18).toString('base64url');
  const server=spawn(process.env.PYTHON||'python',['-u','-m','edudb.web','--demo','--port','0','--db',path.join(artifacts,'db')],
    {cwd:root,windowsHide:true,env:{...process.env,EDUDB_ADMIN_PASSWORD:password},stdio:['ignore','pipe','pipe']});
  const closed=new Promise(resolve=>server.on('exit',resolve));
  let browser;
  try{
    const url=await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(new Error('Server startup timeout')),45000);
      let output='';
      server.stdout.on('data',chunk=>{output+=chunk;const found=output.match(/http:\/\/127\.0\.0\.1:\d+\//);if(found){clearTimeout(timeout);resolve(found[0]);}});
      server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.on('error',reject);server.on('exit',code=>{clearTimeout(timeout);reject(new Error('Server exited: '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.platform==='win32'?{channel:'msedge'}:{})});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url);
    await page.locator('#show-register').click();
    await page.locator('#register-username').fill('registered');
    await page.locator('#register-password').fill(memberPassword);
    await page.locator('#register-confirm').fill(memberPassword);
    await page.locator('#register-form button[type=submit]').click();
    await page.locator('#register-form').waitFor({state:'hidden'});
    await page.locator('#toast').waitFor({state:'hidden'});
    await page.locator('#username').fill('admin');
    await page.locator('#password').fill(password);
    await page.locator('#login-form button[type=submit]').click();
    await page.locator('#app').waitFor({state:'visible'});
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    assert.match(await page.locator('#result-summary').innerText(),/20 条记录/);
    await page.screenshot({path:path.join(artifacts,'workspace-desktop.png'),fullPage:true});
    await page.locator('[data-result="plan"]').click();
    assert.ok(await page.locator('#result-plan .plan-node').count()>2);
    await page.locator('#result-plan .plan-node').last().click();
    assert.match(await page.locator('#plan-detail').innerText(),/SeqScan/);
    await page.screenshot({path:path.join(artifacts,'plan-desktop.png'),fullPage:true});
    await page.locator('#preset').selectOption('createView');
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    await page.locator('#preset').selectOption('view');
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    assert.match(await page.locator('#result-rows').innerText(),/赵六/);
    await page.locator('[data-result="teaching"]').click();
    await page.locator('[data-teaching="views"]').click();
    await page.locator('.lineage-row').first().click();
    assert.match(await page.locator('#lineage-detail').innerText(),/students.id/);
    const expanded=page.locator('#teaching-content .view-plan').first();
    await expanded.locator(':scope > summary').click();
    assert.equal(await expanded.getAttribute('open'),null);
    await expanded.locator(':scope > summary').click();
    await page.evaluate(()=>{window.scrollTo(0,0);document.querySelector('#result-teaching').scrollTop=0;});
    await page.screenshot({path:path.join(artifacts,'view-expansion.png'),fullPage:true});
    await page.locator('[data-teaching="optimizer"]').click();
    let stepCount=0;
    while(await page.locator('[data-opt-move="1"]').isEnabled()){
      await page.locator('[data-opt-move="1"]').click();
      assert.ok(++stepCount<33);
    }
    assert.ok(stepCount>=4);
    assert.match(await page.locator('#teaching-content').innerText(),/IndexScan/);
    assert.ok(await page.locator('.rewrite-highlight').count()>0);
    assert.match(await page.locator('#teaching-content').innerText(),/\[5, 5\]/);
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(artifacts,'optimization-steps.png'),fullPage:true});
    await page.locator('[data-teaching="flow"]').click();
    assert.match(await page.locator('.flow-detail').innerText(),/索引扫描/);
    assert.equal(await page.locator('.flow-node.selected').count(),1);
    await page.locator('[data-flow-move="1"]').click();
    assert.match(await page.locator('.flow-detail').innerText(),/条件筛选/);
    for(const width of [390,768,1440]){
      await page.setViewportSize({width,height:1000});
      for(const tab of ['views','optimizer','flow']){
        await page.locator(`[data-teaching="${tab}"]`).click();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`Teaching overflow: ${tab} at ${width}`);
      }
      if(width===390)await page.screenshot({path:path.join(artifacts,'teaching-mobile.png'),fullPage:true});
    }
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(artifacts,'execution-flow.png'),fullPage:true});
    await page.locator('#explain').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    await page.locator('[data-result="teaching"]').click();
    await page.locator('[data-teaching="flow"]').click();
    assert.match(await page.locator('#teaching-content').innerText(),/EXPLAIN 没有执行查询/);
    await page.locator('#optimizer').uncheck();
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    await page.locator('[data-result="teaching"]').click();
    await page.locator('[data-teaching="optimizer"]').click();
    assert.match(await page.locator('#teaching-content').innerText(),/关闭查询优化/);
    await page.locator('#optimizer').check();
    await page.locator('#sql-editor').fill("SELECT name FROM students WHERE id IN (SELECT student_id FROM enrollments WHERE course='数据库')");
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    await page.locator('[data-result="teaching"]').click();
    await page.locator('[data-teaching="flow"]').click();
    assert.match(await page.locator('.flow-tree').innerText(),/enrollments/);
    assert.match(await page.locator('#teaching-content').innerText(),/多次调用的累计值/);
    await page.locator('#sql-editor').fill('THIS IS NOT SQL');
    await page.locator('#run').click();
    await page.locator('#result-error').waitFor({state:'visible'});
    assert.equal(await page.locator('#result-rows tbody tr').count(),0);
    assert.match(await page.locator('#result-error').innerText(),/SYNTAX_ERROR/);
    await page.locator('#begin-tx').click();
    await page.waitForFunction(()=>document.querySelector('#transaction-badge').textContent.includes('事务进行中'));
    await page.locator('#sql-editor').fill('UPDATE students SET score = -1 WHERE id = 5');
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    await page.locator('#rollback-tx').click();
    await page.waitForFunction(()=>document.querySelector('#transaction-badge').textContent.includes('自动提交'));
    await page.locator('#sql-editor').fill('SELECT score FROM students WHERE id=5');
    await page.locator('#run').click();
    await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='执行成功');
    assert.match(await page.locator('#result-rows').innerText(),/80/);
    await page.locator('[data-view="experiments"]').click();
    await page.locator('#estimate-query').click();
    await page.locator('#estimate-output').getByText('执行前的开销估算').waitFor();
    assert.match(await page.locator('#estimate-output').innerText(),/时间复杂度/);
    await page.locator('#run-compare').click();
    await page.locator('.experiment-verdict').waitFor();
    assert.match(await page.locator('.experiment-verdict').innerText(),/验证通过/);
    assert.equal(await page.locator('.bar-fill').count(),6);
    await page.screenshot({path:path.join(artifacts,'experiments-desktop.png'),fullPage:true});
    const downloadEvent=page.waitForEvent('download');
    await page.locator('#export-report').click();
    const download=await downloadEvent;
    await download.saveAs(path.join(artifacts,'report.html'));
    const report=fs.readFileSync(path.join(artifacts,'report.html'),'utf8');
    assert.match(report,/not_run/);assert.match(report,/优化实验/);assert.ok(!report.includes(password));
    await page.locator('[data-view="access"]').click();
    await page.locator('#new-username').fill('viewer');
    await page.locator('#new-password').fill(memberPassword);
    await page.locator('#create-user-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#user-list').textContent.includes('viewer'));
    await page.locator('#grant-user').selectOption('viewer');
    const options=await page.locator('#grant-table option').evaluateAll(els=>els.map(el=>({text:el.textContent,value:el.value})));
    await page.locator('#grant-table').selectOption(options.find(x=>x.text==='students').value);
    await page.locator('.permission-checks input[value=SELECT]').check();
    await page.locator('#grant-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent==='权限已保存');
    await page.locator('[data-view="audit"]').click();
    await page.locator('#audit-content tbody tr').first().waitFor();
    assert.match(await page.locator('#audit-content').innerText(),/GRANT/);
    await page.screenshot({path:path.join(artifacts,'audit-desktop.png'),fullPage:true});
    for(const width of [390,768,1440]){
      await page.setViewportSize({width,height:844});
      for(const view of ['workspace','experiments','catalog','access','audit']){
        await page.locator(`[data-view="${view}"]`).click();
        await page.waitForTimeout(100);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`Overflow: ${view} at ${width}`);
      }
      if(width===390){await page.locator('[data-view="workspace"]').click();await page.screenshot({path:path.join(artifacts,'workspace-mobile.png'),fullPage:true});}
    }
    await page.locator('#logout').click();
    await page.locator('#login-screen').waitFor({state:'visible'});
    assert.equal(await page.locator('#compare-output').innerText(),'');
    await page.locator('#username').fill('viewer');await page.locator('#password').fill(memberPassword);
    await page.locator('#login-form button[type=submit]').click();await page.locator('#app').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-view="access"]').isVisible(),false);
    assert.ok(!(await page.locator('#table-tree').innerText()).includes('enrollments'));
    await page.locator('#sql-editor').fill('DELETE FROM students');await page.locator('#run').click();
    await page.locator('#result-error').waitFor({state:'visible'});
    assert.match(await page.locator('#result-error').innerText(),/FORBIDDEN/);
    await page.locator('#logout').click();
    await page.locator('#login-screen').waitFor({state:'visible'});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed',artifacts,viewports:[390,768,1440],pageErrors:errors,
      flows:['login','query','plan details','view expansion and lineage','optimization step playback','execution dataflow','explain has no execution','error clears rows','rollback','compare','report download','create user','grant','audit','logout','authorization']},null,2));
  }finally{
    if(browser)await browser.close();
    server.kill();await closed;
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
