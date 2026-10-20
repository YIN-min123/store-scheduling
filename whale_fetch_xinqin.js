// =============================================================
// 鲸品云「整体效率分析」兴勤港惠店 全量订单数(orderNum) 取数片段
// 用法：在已登录 https://whale.zwztf.net 的浏览器标签里执行本片段
//       （QoderWork 可用 builtin_browser 的 javascript_tool 直接跑，
//         返回 {start,end,series:{date:orderNum}} 的 JSON 字符串）。
// 说明：orderNum = 「订单数」列 = 全渠道成交单量（淘鲜达+美团+京东+小程序）。
//       鉴权走前端 localStorage 里的 access_token（Bearer）+ TENANT_ID，
//       若返回 code!=0/424「用户凭证已过期」，先在页面点一次「查询」触发
//       SPA 用 refresh_token 自动换新 token，再重跑本片段。
// 参数：把 START / END 改成要拉的下发日期区间（YYYY-MM-DD）。
// =============================================================
(function () {
  var START = '2026-08-15', END = '2026-10-19';   // 按需改
  var ORG = '190507638909397';                     // 兴勤超市（港惠店）organizationId
  var TENANT = '188035768554677';
  var tk = JSON.parse(localStorage.getItem('midstrage-access_token')).data.content;
  var u = 'https://whale.zwztf.net/api/web/datacenter/performance-efficiency/summary/page'
    + '?size=400&current=1'
    + '&startTime=' + START + '+00:00:00&endTime=' + END + '+23:59:59'
    + '&organizationIds=' + ORG
    + '&pickAreas[]=2&pickAreas[]=0&pickAreas[]=1'
    + '&showStatisticsDate=1&showShopInfo=1&showChannel=0&showDeliveryType=0';
  var x = new XMLHttpRequest();
  x.open('GET', u, false);
  x.withCredentials = true;
  x.setRequestHeader('Authorization', 'Bearer ' + tk);
  x.setRequestHeader('TENANT_ID', TENANT);
  x.send(null);
  var d = JSON.parse(x.responseText);
  if (!d || d.code !== 0 || !d.data) return JSON.stringify({ error: 'auth-or-empty', http: x.status, resp: x.responseText.slice(0, 200) });
  var arr = Array.isArray(d.data) ? d.data : (d.data.records || d.data.list || d.data.rows || []);
  var m = {};
  for (var i = 0; i < arr.length; i++) {
    var dt = ('' + (arr[i].finishDate || '')).slice(0, 10);
    if (dt) m[dt] = arr[i].orderNum;   // 订单数(全量)
  }
  return JSON.stringify({ start: START, end: END, count: Object.keys(m).length, series: m });
})();
