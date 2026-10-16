/* One confirmation and one batch POST for all enabled accounts. */
window.WBBenefits = (() => {
  const base = '/api/benefits/compensation';
  let data, generation = 0, automaticBusy = false;
  const node = () => document.getElementById('compensationPanel');
  async function request(path, body, method = 'POST') {
    const response = await api(base + path, {method, ...(body === undefined ? {} : {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
    const parsed = await safeJson(response);
    if (!response.ok || !parsed.ok) throw new Error(parsed.data?.detail || '请求失败，请稍后重试');
    return parsed.data;
  }
  function render() {
    if (!node() || !data) return;
    const {campaign, automatic, items} = data;
    node().innerHTML = `<h3 class="font-bold mb-2">${esc(campaign.title)} · ${Number(campaign.credit)} 积分</h3>
      <p class="benefit-muted">请于 2026/10/22 23:59:59（北京时间）前领取。资格及到账结果以官方为准。</p>
      <div class="benefit-settings"><label><input id="benefitAutomatic" type="checkbox" ${automatic.enabled ? 'checked' : ''} ${campaign.expired || automaticBusy ? 'disabled' : ''} onchange="WBBenefits.automatic(this.checked)"> 自动领取（每 ${Number(automatic.interval_minutes)} 分钟检查）</label></div>
      <p class="benefit-muted">${campaign.expired ? '活动已截止，自动领取已停止。' : '自动覆盖全部启用账号，账号忙碌时顺延。领取后刷新余额与到期快照。'}</p>
      <div class="benefit-summary"><span>启用账号 <b>${items.filter(x => x.enabled).length}</b></span><span>已确认领取 <b>${items.filter(x => x.claimed).length}</b></span><span>待核对 <b>${items.filter(x => x.enabled && !x.claimed).length}</b></span></div>
      <p class="benefit-muted">以上为本地最近记录，执行时会先核对官方状态，已领取账号自动跳过。</p>
      <div class="benefit-toolbar"><button class="console-btn primary" data-account-batch onclick="WBBenefits.claimAll()" ${campaign.expired || !items.some(x => x.enabled) ? 'disabled' : ''}>一键领取全部账号</button>
        <a href="https://www.workbuddy.cn/profile/plans-usage?action=addon" target="_blank" rel="noopener noreferrer">官方权益页面 ↗</a></div>`;
    if (typeof syncAccountBatchButtons === 'function') syncAccountBatchButtons();
  }
  async function open() {
    const ticket = ++generation;
    openModal('<div id="compensationPanel"><p class="benefit-muted">正在读取补偿权益…</p></div>', 'max-w-xl');
    try {
      const result = await request('', undefined, 'GET');
      if (ticket !== generation || !node()) return;
      data = result; render();
    } catch (error) {
      if (ticket !== generation || !node()) return;
      node().innerHTML = '<p class="benefit-muted">补偿信息加载失败，请关闭后重试。</p>';
      toast(esc(String(error.message)), 'err');
    }
  }
  async function automatic(enabled) {
    if (automaticBusy) return;
    automaticBusy = true;
    const control = document.getElementById('benefitAutomatic');
    if (control) control.disabled = true;
    try {
      const result = await request('/automatic', {enabled}, 'PUT');
      if (data) data.automatic.enabled = result.enabled;
      toast(result.enabled ? '已开启自动领取' : '已关闭自动领取', 'ok');
    } catch (error) { toast(esc(String(error.message)), 'err'); }
    finally { automaticBusy = false; render(); }
  }
  async function claimAll() {
    return accountBatchAction(async () => {
      if (!await uiConfirm({kind:'info', danger:false, title:'一键领取 DeepSeek 补偿',
        message:'对全部启用账号领取 500 积分补偿？已领取账号会跳过。请于 10/22 前领取。', okText:'确认一键领取'})) return;
      await runJob({title:'DeepSeek 补偿权益批量领取', iconHtml:'', submitUrl:base + '/batch', body:{confirm:true},
        label:item => item.account || item.account_id, detail:item => item.reason || '',
        amount:item => item.credit > 0 ? `<span class="text-emerald-600">+${Number(item.credit)}</span>` : '',
        icon:item => item.ok ? '<i class="fa-solid fa-circle-check text-emerald-500"></i>' : '<i class="fa-solid fa-clock text-amber-600"></i>',
        doneText:job => job.result?.summary || '批量领取已结束', summaryHtml:() => '',
        doneToast:job => job.result?.summary || '批量领取已结束',
        onFinish:() => { loadAccounts(); if (_creditStats) loadCreditStats(); }
      });
    });
  }
  return {open, automatic, claimAll};
})();
