const $=s=>document.querySelector(s), app=$('#app');
const GLOSS={"vs index": "Median alpha measured against SPY over the identical window. The benchmark is the whole point: +6% in a window where SPY did +7% is behind the market.", "vs sector": "The same alpha measured against the trade's own sector ETF instead of SPY. It separates picking a stock from riding a sector. Unmapped sectors fall back to SPY, and on fund holdings this column is noise, because a fund inherits its sponsor's SIC code.", "beat index": "The share of this member's scored trades whose alpha against the index came out positive. A hit rate, not a size.", "alpha": "Excess return in the direction the member took: a buy scores stock minus benchmark, a sell scores benchmark minus stock. So exiting a name that then lagged the market counts as a win.", "90d ret": "The stock's own raw return over the 90 days after disclosure, before any benchmark is subtracted. Read it next to the benchmark, never alone.", "top name": "The share of this member's scored trades sitting in their single most-traded ticker. A high share means the record is really one bet.", "1st half": "The same member scored separately on the older and the newer half of their own trades. A big gap between the halves means the record is not stable.", "filing lag": "Days between the transaction and its disclosure. The STOCK Act allows 45.", "party unity": "The share of this member's yea/nay votes matching their own party's majority, computed from raw Voteview roll calls.", "DW-NOMINATE": "Voteview's ideology score on the first dimension. Negative is left, positive is right.", "net flow": "Disclosed buying minus disclosed selling for a name. Positive means Congress accumulated it on net; negative means it was sold down.", "disclosed": "The date the filing was made public. Everything here is measured from this date, not the trade date, because nobody outside the filing knew until then.", "disclosure date": "The date the filing was made public. Returns are measured from here, because trade-date returns would flatter these members and mean nothing.", "owner": "Who holds the asset: Self, Spouse, Joint or Dependent.", "amount": "Disclosures give a dollar bracket, never an exact figure. Every total here uses the bracket's lower bound, so it is a floor.", "min amount": "Only count disclosures whose bracket floor is at least this. $1,001-$15,000 is mostly rebalancing noise.", "horizon": "The forward window, in days, over which each return is measured.", "walk-forward": "Re-rank the members every year and grade them on the next one, pooling the folds. Harder to fool than a single split.", "measurable": "A disclosure with a ticker and a closed price window, so a return can actually be computed. Many filings have neither.", "distinct members": "How many different people traded the name. The convergence signal, and the hardest to skew with one heavy trader.", "dollar volume": "Disclosed dollars traded in a name, buys and sells added together. Bracket floors, so a lower bound.", "trade count": "The number of disclosures naming this stock.", "PTR": "Periodic Transaction Report: the filing a member must make within 45 days of a securities trade.", "SPY": "The S&P 500 ETF, used here as the index benchmark.", "sector ETF": "A fund tracking the trade's own industry, used as the second benchmark. The SIC-to-ETF mapping is a judgement call, not a definition.", "min trades": "Members below this many measurable trades are left out entirely: under about ten, a record is one lucky quarter.", "priced": "Disclosures for which a forward return could be computed, because the ticker resolved and the window has closed."};
const pct=v=>v==null?'':(v>0?'+':'')+(v*100).toFixed(1)+'%';
// Sign is a mark, not a hue: see common.css.
const cls=v=>v==null?'num':(v>0?'num up':v<0?'num down':'num');
const dirTag=t=>{ const k=(t||'').toLowerCase(); const c=k.startsWith('b')?'buy':k.startsWith('s')?'sell':'exchange';
  return `<span class="tag ${c}">${esc(t||'')}</span>`; };
let facets={members:[],tickers:[]};

// --- routing ---------------------------------------------------------------
// Four places you go daily, then every report in one grouped menu, and Maintenance
// set apart. Descriptions come from the server: TABS for the fixed tabs, each
// report's own blurb for the rest. A read-only portal has no Watchlist or Maintenance.
const PRIMARY=['','trends','explore','watch'];
const GROUPS=[['performance','Performance'],['accountability','Accountability'],
  ['briefings','Briefings & data']];
function nav(){
  const cur=location.hash.replace(/^#\//,'');
  const tab=(h,t,b)=>`<a href="#/${h}" data-tip="${esc(b)}" aria-description="${esc(b)}"${
    h===cur?' aria-current="page"':''}>${esc(t)}</a>`;
  const prim=PRIMARY.filter(h=>!(READ_ONLY&&h==='watch')).map(h=>tab(h,TABS[h].title,TABS[h].blurb));
  const inMenu=cur.startsWith('r/')||cur==='page';
  const here=cur==='page'?TABS.page.title:(REPORTS[cur.slice(2)]||{}).title;
  const groups=GROUPS.map(([g,label])=>{
    const items=Object.entries(REPORTS).filter(([,v])=>v.group===g).map(([k,v])=>tab('r/'+k,v.title,v.blurb));
    if(g==='briefings') items.push(tab('page',TABS.page.title,TABS.page.blurb));
    return `<div class="menu-group"><h4>${label}</h4>${items.join('')}</div>`;}).join('');
  $('#nav').innerHTML=prim.join('')+
    `<details class="menu"><summary${inMenu?' aria-current="page"':''}>Reports${
      inMenu&&here?`<span class="here">: ${esc(here)}</span>`:''}</summary>
      <div class="menu-list">${groups}</div></details>`+
    (READ_ONLY?'':'<span class="nav-gap"></span>'+tab('jobs',TABS.jobs.title,TABS.jobs.blurb));
}
// The menu closes on any click outside it, and on choosing a report.
document.addEventListener('click',e=>{ document.querySelectorAll('details.menu[open]').forEach(d=>{
  if(!d.contains(e.target)||e.target.closest('a')) d.removeAttribute('open'); }); });
async function route(){
  nav();
  // The panel is fixed-position, so it would otherwise hang over the next view.
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const cur=location.hash.replace(/^#\//,'');
  if(cur==='trends') return trends();
  if(cur==='explore') return explore();
  if(cur==='watch'&&!READ_ONLY) return watchPage();
  if(cur==='page') return full();
  if(cur==='jobs'&&!READ_ONLY) return jobs();
  if(cur.startsWith('r/')) return report(cur.slice(2));
  return overview();
}
addEventListener('hashchange',route);

// Ticker -> full name, for hover text. Fetched once; a hover before it lands
// just shows the bare symbol.
let NAMES={};
const namesP=fetch('/api/names').then(r=>r.json()).then(d=>{NAMES=d;}).catch(()=>{});
const tkTitle=i=>{const t=i[0].label; return NAMES[t]?`${t} - ${NAMES[t]}`:t;};

// --- overview --------------------------------------------------------------
async function overview(){
  app.innerHTML='<div class="spin">loading...</div>';
  const s=await (await fetch('/api/stats')).json();
  if(!s||!s.trades){ app.innerHTML='<div class="err"><b>No data yet.</b><p>Press '+
    '<b>Refresh data</b> above. The first pass takes 15-25 minutes, almost all of it '+
    'rate-limited price fetches.</p></div>'; return; }
  const tiles=[['trades','disclosures'],['members','members'],['priced','with 90d returns'],
    ['tickers','distinct tickers']].map(([k,l])=>
    `<div class="stat"><b>${(s[k]||0).toLocaleString()}</b><span>${l}</span></div>`).join('')
    +(s.latest?`<div class="stat"><b>${esc(s.latest)}</b><span>latest disclosure</span></div>`:'');
  const cards=Object.entries(REPORTS).map(([k,v])=>
    `<a class="card" href="#/r/${k}"><b>${esc(v.title)}</b><span>${esc(v.blurb)}</span></a>`).join('');
  app.innerHTML=`<div class="stats">${tiles}</div>${persistenceCallout(s.persistence)}
    <div class="chartbox"><h2>Recent activity</h2>
      <p class="cap">Disclosures per month over the last two years: buys above the line, sells below.
        <a href="#/trends">Open Trends</a> for top names and a longer window.</p>
      <div class="canvas-wrap" id="w-act"><canvas id="c-act"></canvas></div></div>
    <p><a href="#/explore"><b>Explore every disclosure</b></a> - filter and sort all
    ${(s.trades||0).toLocaleString()} of them live, and click any member for their record.</p>
    <h2>Reports</h2><div class="cards">${cards}</div>
    <h2>Reading any of this</h2><p>Returns are measured from the <b>disclosure</b> date,
    not the trade date, and <b>vs sector</b> is the honest column: roughly half the apparent
    edge across scored members is sector exposure, not stock selection. Sector alpha on fund
    holdings is noise, since a fund inherits its sponsor's SIC code. Past alpha barely
    predicts future alpha.</p>`;
  annotate(app);
  drawActivity();
}

// The finding that governs how every ranking here should be read. Live from the
// data, so the wording can never outlive the number.
function persistenceCallout(p){
  if(!p||p.r==null) return '';
  const weak=Math.abs(p.r)<0.25;
  return `<div class="callout"><b>${weak?'Past records have not predicted future ones.':
    'Records show some persistence, from one sample.'}</b> Splitting each member's
    scored trades in half, the older half's alpha and the newer half's correlate at
    <span class="k">r = ${p.r>=0?'+':''}${p.r.toFixed(2)}</span> across ${p.n} members, and
    ${Math.round(100*p.same_sign)}% keep the same sign${weak?' &mdash; a coin flip':''}.
    Read the rankings as history, not as tips. <a href="#/r/backtest">The backtest</a>
    tests it out of sample.</div>`;
}

async function drawActivity(){
  const d=await (await fetch('/api/timeline?days=730')).json();
  if(!d.rows||!d.rows.length){ const w=$('#w-act');
    if(w) w.innerHTML='<div class="nochart">Nothing to plot yet.</div>'; return; }
  const r=d.rows;
  $('#w-act').style.height='250px';
  monthBars('c-act',r,r.map(x=>x.buys),r.map(x=>x.sells),v=>v.toLocaleString(),
    {click:clickMonth(r,d.since)});
}

// Monthly buying and selling as one diverging column per month: buys above the
// zero line, sells below. One measure with two poles, so one axis; the position
// carries direction as well as the colour does. Counts are months, not a
// continuous signal, so bars rather than a smoothed line.
const MON=ym=>new Date(ym+'-01T12:00:00').toLocaleString(undefined,{month:'short'})+' '+ym.slice(2,4);
function monthBars(id,r,buy,sell,fmt,extra){
  const bar=(label,data,color)=>({label,data,backgroundColor:color,borderWidth:0,
    borderRadius:4,borderSkipped:'start',categoryPercentage:.86,barPercentage:.92});
  paint(id,{type:'bar',data:{labels:r.map(x=>x.ym),
      datasets:[bar('Buys',buy,C.buy),bar('Sells',sell.map(v=>-v),C.sell)]},
    options:{maintainAspectRatio:false,responsive:true,
      interaction:{mode:'index',intersect:false},
      ...extra.click,
      plugins:{legend:{display:true,position:'top',align:'end',
          labels:{boxWidth:9,boxHeight:9,usePointStyle:true,pointStyle:'rect',padding:14}},
        tooltip:Object.assign({},tip,{callbacks:Object.assign({
          title:i=>MON(i[0].label),
          label:c=>`${c.dataset.label}: ${fmt(Math.abs(c.parsed.y))}`,
          footer:()=>'click a bar to list them'},extra.callbacks||{})})},
      scales:{x:axis({stacked:true,grid:{display:false},ticks:{color:C.faint,maxRotation:0,
          autoSkipPadding:14,callback:function(v){return MON(this.getLabelForValue(v));}}}),
        y:axis({stacked:true,ticks:{color:C.faint,padding:6,callback:v=>fmt(Math.abs(v))},
          grid:{color:c=>c.tick.value===0?C.faint:C.grid,drawTicks:false}})}}});
}

// A member's name in their party's colour; '' leaves an unknown party as it was.

// Both timelines open the explorer on one month and direction. `since` is the
// window's own cut, because the first month plotted is usually partial.
function openMonth(ym,sell,since,extra){
  Object.assign(st,{q:'',member:'',ticker:'',chamber:'',floor:'',tickered:'0',watched:'0',
    type:sell?'sell':'buy',since:[ym+'-01',since||''].sort()[1],until:ym+'-31',
    sort:'date',dir:'desc',offset:0},extra||{});
  if(location.hash!=='#/explore') location.hash='#/explore'; else explore();
}
function clickMonth(r,since,extra){ return {
  onHover:(e,els)=>{e.native.target.style.cursor=els.length?'pointer':'default';},
  onClick:(e,els,chart)=>{
    const hit=chart.getElementsAtEventForMode(e,'nearest',{intersect:false,axis:'xy'},true)[0];
    if(hit) openMonth(r[hit.index].ym,hit.datasetIndex===1,since,extra);}};
}

// --- explorer --------------------------------------------------------------
const st={q:'',member:'',ticker:'',chamber:'',type:'',floor:'',since:'',until:'',tickered:'0',watched:'0',
  sort:'date',dir:'desc',offset:0,limit:100};
let timer=null;

async function explore(){
  if(!facets.members.length) facets=await (await fetch('/api/facets')).json();
  // On a phone the results come first: the filters fold into one line that says
  // how many are set. On a desk they stay open.
  const nset=['q','member','ticker','chamber','type','floor','since','until'].filter(k=>st[k]).length
    +(st.tickered==='1')+(st.watched==='1');
  app.innerHTML=`<details class="fbox"${innerWidth>700?' open':''}><summary>Filters${
    nset?` <span class="note">(${nset} set)</span>`:''}</summary><div class="filters">
    <div><label>search</label><input id="f-q" placeholder="member, ticker, asset" value="${esc(st.q)}"></div>
    <div><label>member</label><select id="f-member"><option value="">any</option>${
      facets.members.map(m=>`<option value="${esc(m.member)}"${st.member===m.member?' selected':''}>${
        esc(m.full_name||m.member)} (${m.n})</option>`).join('')}</select></div>
    <div><label>ticker</label><select id="f-ticker"><option value="">any</option>${
      facets.tickers.map(t=>`<option value="${esc(t.ticker)}"${st.ticker===t.ticker?' selected':''}>${
        esc(t.ticker)} (${t.n})</option>`).join('')}</select></div>
    <div><label>chamber</label><select id="f-chamber">${
      ['','House','Senate'].map(c=>`<option value="${c}"${st.chamber===c?' selected':''}>${c||'both'}</option>`).join('')}</select></div>
    <div><label>type</label><select id="f-type">${
      ['','buy','sell'].map(c=>`<option value="${c}"${st.type===c?' selected':''}>${c||'any'}</option>`).join('')}</select></div>
    <div><label>min amount</label><input id="f-floor" inputmode="numeric" placeholder="15001" value="${esc(st.floor)}"></div>
    <div><label>since</label><input id="f-since" placeholder="2025-01-01" value="${esc(st.since)}"></div>
    <div><label>until</label><input id="f-until" placeholder="2025-12-31" value="${esc(st.until)}"></div>
    <div class="chk"><input type="checkbox" id="f-tickered"${st.tickered==='1'?' checked':''}>
      <label style="margin:0;text-transform:none;font-size:13px">tickered only</label></div>
    ${READ_ONLY?'':`<div class="chk"><input type="checkbox" id="f-watched"${st.watched==='1'?' checked':''}>
      <label style="margin:0;text-transform:none;font-size:13px">watched only</label></div>`}
  </div></details><div id="rows"><div class="spin">loading...</div></div>`;

  const bind=(id,key,ev)=>{const el=$(id); if(!el) return;
    el.addEventListener(ev,()=>{ st[key]=el.type==='checkbox'?(el.checked?'1':'0'):el.value;
      st.offset=0; clearTimeout(timer); timer=setTimeout(rows,ev==='input'?260:0); });};
  bind('#f-q','q','input'); bind('#f-member','member','change');
  bind('#f-ticker','ticker','change'); bind('#f-chamber','chamber','change');
  bind('#f-type','type','change'); bind('#f-floor','floor','input');
  bind('#f-since','since','input'); bind('#f-until','until','input');
  bind('#f-tickered','tickered','change'); bind('#f-watched','watched','change');
  rows();
}

async function rows(){
  const p=new URLSearchParams(Object.fromEntries(
    Object.entries(st).filter(([k,v])=>v!=='' && v!=='0' || k==='offset' || k==='limit')));
  const box=$('#rows'); if(!box) return;
  const [d]=await Promise.all([fetch('/api/trades?'+p).then(x=>x.json()),namesP]);
  if(d.error){ box.innerHTML=`<div class="err">${esc(d.error)}</div>`; return; }
  if(!d.rows.length){ box.innerHTML='<p class="note">Nothing matches those filters.</p>'; return; }
  const cols=[['date','disclosed'],['member','member'],['ticker','ticker'],['','asset'],
    ['','type'],['amount','amount'],['ret','90d ret'],['alpha','alpha']];
  const th=cols.map(([k,l])=>k?`<th class="s" data-k="${k}"${st.sort===k?` data-on="${
    st.dir==='asc'?'^':'v'}"`:''}>${l}</th>`:`<th>${l}</th>`).join('');
  const body=d.rows.map(r=>`<tr>
    <td class="num">${esc(r.disclosed||r.tx_date||'')}</td>
    <td><span class="mlink ${pc(r.party)}" data-m="${esc(r.member)}">${esc(r.full_name||r.member)}</span>${
      r.party?` <span class="note">${esc(r.party[0])}</span>`:''}</td>
    <td>${r.ticker?`<b class="mlink" data-t="${esc(r.ticker)}" title="${esc(NAMES[r.ticker]||'')}">${esc(r.ticker)}</b>`
      :'<span class="note">-</span>'}</td>
    <td>${esc((r.asset_name||'').slice(0,54))}</td>
    <td>${dirTag(r.tx_type)}</td>
    <td class="num">${esc(r.amount_range||'')}</td>
    <td class="${cls(r.ret_90)}">${pct(r.ret_90)}</td>
    <td class="${cls(r.alpha)}">${pct(r.alpha)}</td></tr>`).join('');
  const from=d.offset+1, to=Math.min(d.offset+d.limit,d.total);
  box.innerHTML=`<div class="pager"><b>${d.total.toLocaleString()}</b> matching -
      showing ${from.toLocaleString()}-${to.toLocaleString()}
      <button id="prev"${d.offset?'':' disabled'}>prev</button>
      <button id="next"${to>=d.total?' disabled':''}>next</button></div>
    <div class="tbl-scroll"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
  box.querySelectorAll('th.s').forEach(h=>h.onclick=()=>{
    const k=h.dataset.k;
    st.dir=(st.sort===k&&st.dir==='desc')?'asc':'desc'; st.sort=k; st.offset=0; rows();});
  box.querySelectorAll('.mlink').forEach(e=>e.onclick=()=>
    e.dataset.t?tickerPanel(e.dataset.t):member(e.dataset.m));
  annotate(box.querySelector('thead'));
  const prev=$('#prev'), next=$('#next');
  if(prev) prev.onclick=()=>{st.offset=Math.max(0,st.offset-st.limit); rows();};
  if(next) next.onclick=()=>{st.offset+=st.limit; rows();};
}

// The price line is the context the disclosure counts lack: bars tell you when
// Congress moved, this tells you what the name was doing when they did. Same line
// treatment as Recent activity on the overview, so the two read as one chart type.
function drawPrice(d){
  const p=d.price; if(!p||!(p.labels||[]).length) return;
  const up=(d.agg.net||0)>=0, line=up?C.buy:C.sell;
  const grain=p.bucket==='day'?'Daily closes'
    :p.bucket==='week'?'Weekly closes' : 'Monthly closes';
  // On a name Congress discloses most weeks, ringing every bucket draws the line
  // twice and says nothing. There the rings thin out to the heaviest periods; the
  // bars below still carry the full timing, and a hover still reports every count.
  const hit=p.labels.filter((_,i)=>p.buys[i]||p.sells[i]).length;
  const dense=hit>p.labels.length*.45;
  const nz=p.buys.concat(p.sells).filter(n=>n>0).sort((a,b)=>a-b);
  const cut=dense?Math.max(2,nz[Math.floor(nz.length*.75)]||2):1;
  const keep=arr=>arr.map(n=>n>=cut?n:0);
  const cap=$('#px-cap');
  if(cap) cap.textContent=`${grain} from the last refresh, over the span this name has `+
    `been disclosed in. The line is ${up?'aqua: Congress is a net buyer'
      :'orange: Congress is a net seller'} of it. `+(dense
      ? `It is disclosed in most ${p.bucket==='month'?'months':p.bucket==='week'?'weeks':'sessions'}, `+
        `so only periods of ${cut} or more are marked -- aqua circles are buys, just `+
        `under the line, orange diamonds sells, just above it. Hover any point for the `+
        `full count.`
      : `Marks sit where the disclosures land, sized by how many: aqua circles are `+
        `buys, just under the line, orange diamonds sells, just above it.`);
  // A marker sits on the close, so it reads as a point on the line rather than a
  // second series floating beside it. Radius grows with the square root of the
  // count: area, not radius, is what the eye compares. The rings are hollow and
  // the price line is drawn last, on top of them -- on a name Congress trades
  // every week a wall of filled dots erases the very line it is annotating.
  const dot=n=>n?Math.min(2.6+Math.sqrt(n),6.5):0;
  // Buys ride just under the close and sells just above it, the way a trading
  // chart marks them: a day that carries both would otherwise stack one marker
  // exactly on the other and show only whichever drew last.
  const lo=Math.min(...p.close), hi=Math.max(...p.close), off=(hi-lo)*.03||.01;
  const at=(arr,d)=>p.labels.map((_,i)=>arr[i]?p.close[i]+d*off:null);
  const marker=(arr,color,style,d)=>({data:at(arr,d),showLine:false,pointStyle:style,
    pointRadius:arr.map(dot),pointHoverRadius:arr.map(n=>n?dot(n)+2:0),
    pointBackgroundColor:C.surface,pointBorderColor:color,pointBorderWidth:1.6,
    borderColor:color,backgroundColor:color});
  // The line is drawn first and faded: it is the backdrop, and at full strength a
  // aqua net-buyer's line swallows the aqua buy rings it is meant to carry. The
  // markers keep the full hue, because that is where aqua and orange have to mean
  // buy and sell.
  paint('c-px',{type:'line',data:{labels:p.labels,
      datasets:[{label:'Close',data:p.close,borderColor:fade(line,.5),
          backgroundColor:fade(line,.5),borderWidth:1.6,pointRadius:0,
          pointHoverRadius:5,tension:.25},
        Object.assign({label:'Buys'},marker(keep(p.buys),C.buy,'circle',-1)),
        Object.assign({label:'Sells'},marker(keep(p.sells),C.sell,'rectRot',1))]},
    options:{maintainAspectRatio:false,responsive:true,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:true,position:'top',align:'end',
          labels:{boxWidth:9,boxHeight:9,usePointStyle:true,padding:12,
            filter:i=>i.text!=='Close'}},
        // Counts come from the data rather than from the markers, so a bucket
        // whose marker was thinned away still answers when you hover it.
        tooltip:Object.assign({},tip,{filter:i=>i.datasetIndex===0,callbacks:{
          title:i=>p.asof[i[0].dataIndex],
          label:c=>`close ${px(p.close[c.dataIndex])}`,
          afterBody:i=>{const k=i[0].dataIndex, out=[];
            if(p.buys[k]) out.push(`${p.buys[k]} buy${p.buys[k]===1?'':'s'} disclosed`);
            if(p.sells[k]) out.push(`${p.sells[k]} sell${p.sells[k]===1?'':'s'} disclosed`);
            return out;}}})},
      scales:{x:axis({grid:{display:false},ticks:{color:C.faint,maxTicksLimit:6,
          callback:function(v){const l=p.labels[v]||''; return p.bucket==='month'?l:l.slice(0,7);}}}),
        // One precision for the whole axis: px() drops cents above $100, which on a
        // scale crossing it prints $240 next to $80.00.
        y:axis({ticks:{color:C.faint,padding:6,
          callback:v=>hi<100?'$'+v.toFixed(2):'$'+Math.round(v).toLocaleString()}})}}});
}

// The streams beyond trades, for one person. Each line names its source report.
function otherDisclosures(d){
  const c=d.compliance, f=d.finance, a=d.annual, rows=[];
  if(c&&c.late_count) rows.push(`<b>filed late</b><span>${c.late_count} of ${c.total_trades} `+
    `past the 45-day deadline (${Math.round(100*c.late_share)}%), worst ${c.worst_lag} days</span>`);
  else if(c) rows.push(`<b>filed late</b><span>never, across ${c.total_trades}</span>`);
  if(f) rows.push(`<b>PAC money</b><span>${money(f.pac_dollars)} from PACs in sectors their `+
    `committees oversee (${esc((f.sectors||[]).join(', '))}), of ${money(f.pac_total)} PAC total; `+
    `${f.trade_count} trades in those sectors</span>`);
  if(a){
    if(a.debts) rows.push(`<b>debts</b><span>${a.debts} liabilities, at least ${money(a.debt_min)}</span>`);
    if(a.income) rows.push(`<b>outside income</b><span>${money(a.income)}</span>`);
    if((a.positions||[]).length) rows.push(`<b>positions held</b><span>${a.positions.map(esc).join('; ')}</span>`);
  }
  if(!rows.length) return '';
  return `<h3>Beyond trades</h3><div class="kv">${rows.join('')}</div>
    <p class="note">From the Compliance, Finance and Annual reports. Annual reports
    cover only the filings fetched so far${a?` (${a.filings} for this member)`:''}.</p>`;
}

async function member(name){
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const d=await (await fetch('/api/member?name='+encodeURIComponent(name))).json();
  const a=d.agg||{}, el=document.createElement('div');
  el.className='panel';
  el.innerHTML=`<button class="x" id="closep">close</button>
    ${d.wiki_thumb?`<img src="${esc(d.wiki_thumb)}" alt="">`:''}
    <h2 class="${pc(d.party)}">${esc(d.full_name||name)}</h2>
    <p class="note">${esc([d.party,d.chamber,d.state,d.district?'district '+d.district:''].filter(Boolean).join(' - '))}</p>
    ${d.wiki_desc?`<p>${esc(d.wiki_desc)}</p>`:''}
    <div class="kv">
      <b>disclosures</b><span>${(a.n||0).toLocaleString()}</span>
      <b>buys / sells</b><span>${a.buys||0} / ${a.sells||0}</span>
      <b>disclosed volume</b><span>at least $${(a.vol||0).toLocaleString()}</span>
      ${d.score&&d.score.n!=null
        ?`<b>alpha vs index</b><span>${pct(d.score.med)} median over ${d.score.n} scored trades</span>
          <b>vs sector</b><span>${pct(d.score.smed)}</span>
          <b>beat index</b><span>${Math.round(100*d.score.beat)}% of trades</span>`
        :`<b>alpha</b><span>not scored: fewer than ${d.score?d.score.min:10} measurable
          trades above the default floor</span>`}
      <b>mean filing lag</b><span>${a.lag==null?'-':Math.round(a.lag)+' days'}</span>
      ${d.party_unity!=null?`<b>party unity</b><span>${(d.party_unity).toFixed(0)}%</span>`:''}
      ${d.nominate!=null?`<b>DW-NOMINATE</b><span>${d.nominate.toFixed(2)}</span>`:''}
    </div>
    ${otherDisclosures(d)}
    ${(d.top||[]).length?`<h3>Most-traded</h3>
      <p class="note">Bar length is how many disclosures; aqua is net buying, orange net selling.</p>
      <div class="canvas-wrap" id="w-mem" style="height:${Math.min(d.top.length,12)*22+30}px">
        <canvas id="c-mem"></canvas></div>`:''}
    ${(d.committees||[]).length?`<h3>Committees</h3><div>${d.committees.map(c=>
      `<span class="pill">${esc(c.name||'')}</span>`).join('')}</div>`:''}
    ${d.wiki_url?`<p style="margin-top:1rem"><a href="${esc(d.wiki_url)}" target="_blank" rel="noopener">Wikipedia</a></p>`:''}
    <p><button id="onlyme">show only their trades</button>
      <button id="watchme" data-kind="member" data-value="${esc(name)}"></button></p>`;
  document.body.appendChild(el);
  annotate(el);
  if((d.top||[]).length){
    // Length is how often they traded the name; colour is which way it went on
    // balance -- aqua net buying, orange net selling -- the same rule the trend
    // charts use, so a reader carries one reading across the whole portal.
    paint('c-mem',{type:'bar',data:{labels:d.top.map(t=>t.ticker),
        datasets:[{data:d.top.map(t=>t.n),
          backgroundColor:d.top.map(t=>(t.net||0)>=0?C.buy:C.sell),borderWidth:0,
          borderRadius:3,borderSkipped:'start',barPercentage:.8,categoryPercentage:.9}]},
      options:{indexAxis:'y',maintainAspectRatio:false,responsive:true,
        onClick:(ev,els)=>{ if(els&&els.length) tickerPanel(d.top[els[0].index].ticker); },
        onHover:(ev,els)=>{ ev.native.target.style.cursor=els.length?'pointer':'default'; },
        plugins:{legend:{display:false},tooltip:Object.assign({},tip,{callbacks:{
          title:tkTitle,
          label:c=>{const t=d.top[c.dataIndex];
            return [`${c.parsed.x} disclosure${c.parsed.x===1?'':'s'}`,
              `${t.net>0?'net buying':t.net<0?'net selling':'balanced'}: ${money(t.net||0)}`];}}})},
        scales:{x:axis({ticks:{color:C.faint,padding:4,precision:0}}),
          y:axis({grid:{display:false},ticks:{color:C.ink,padding:4}})}}});
  }
  $('#closep').onclick=()=>el.remove();
  $('#onlyme').onclick=()=>{ st.member=name; st.offset=0; el.remove();
    if(location.hash!=='#/explore') location.hash='#/explore'; else explore(); };
  watchButton($('#watchme'));
}

// --- glossary hover layer --------------------------------------------------
// The jargon here is load-bearing -- "vs index" and "vs sector" mean different
// things and the gap between them is the whole story -- so every term explains
// itself in place rather than in a legend nobody scrolls to.
const tipbox=(()=>{const d=document.createElement('div'); d.id='tipbox';
  document.body.appendChild(d); return d;})();
let tiphide=null;
// A glossary term (data-g) or anything carrying its own text (data-tip: the tabs).
function showTip(el){
  const k=el.dataset.g||el.textContent.trim(), txt=el.dataset.g?GLOSS[k]:el.dataset.tip;
  if(!txt) return;
  clearTimeout(tiphide);
  tipbox.innerHTML=`<b>${esc(k)}</b>${esc(txt)}`;
  tipbox.classList.add('on');
  const r=el.getBoundingClientRect(), w=Math.min(330,innerWidth-24);
  tipbox.style.width=w+'px';
  const bh=tipbox.offsetHeight;
  let top=r.bottom+8; if(top+bh>innerHeight-8) top=Math.max(8,r.top-bh-8);
  tipbox.style.top=top+'px';
  tipbox.style.left=Math.max(12,Math.min(r.left,innerWidth-w-12))+'px';
}
function hideTip(){ tiphide=setTimeout(()=>tipbox.classList.remove('on'),80); }

const rxSafe=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const GKEYS=Object.keys(GLOSS).sort((a,b)=>b.length-a.length);
const GRX=new RegExp('(?<![\\w-])('+GKEYS.map(rxSafe).join('|')+')(?![\\w-])','gi');

// Wraps known terms wherever they appear as text. Capped per term so a long
// report does not turn into a field of dotted underlines.
function annotate(root){
  if(!root) return;
  const seen={};
  const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode(n){
    if(!n.nodeValue||!n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
    const p=n.parentElement;
    if(!p||p.closest('.gloss,a,code,script,style,option,#tipbox'))
      return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT;}});
  const jobs=[]; let n;
  while((n=walk.nextNode())) if(GRX.test(n.nodeValue)){ GRX.lastIndex=0; jobs.push(n); }
  jobs.forEach(node=>{
    const txt=node.nodeValue; let out=null, last=0; GRX.lastIndex=0; let m;
    while((m=GRX.exec(txt))){
      const key=GKEYS.find(k=>k.toLowerCase()===m[1].toLowerCase());
      if(!key) continue;
      seen[key]=(seen[key]||0)+1;
      if(seen[key]>2) continue;                 // first two mentions only
      out=out||document.createDocumentFragment();
      if(m.index>last) out.appendChild(document.createTextNode(txt.slice(last,m.index)));
      const sp=document.createElement('span');
      sp.className='gloss'; sp.dataset.g=key; sp.tabIndex=0;
      sp.setAttribute('aria-label',m[1]+': '+GLOSS[key]);
      sp.textContent=m[1];
      out.appendChild(sp); last=m.index+m[1].length;
    }
    if(out){ if(last<txt.length) out.appendChild(document.createTextNode(txt.slice(last)));
      node.parentNode.replaceChild(out,node); }
  });
}
const TIPPED='.gloss,[data-tip]';
document.addEventListener('mouseover',e=>{
  const g=e.target.closest&&e.target.closest(TIPPED); if(g) showTip(g);});
document.addEventListener('mouseout',e=>{
  if(e.target.closest&&e.target.closest(TIPPED)) hideTip();});
document.addEventListener('focusin',e=>{
  const g=e.target.closest&&e.target.closest(TIPPED); if(g) showTip(g);});
document.addEventListener('focusout',e=>{
  if(e.target.closest&&e.target.closest(TIPPED)) hideTip();});
addEventListener('scroll',()=>tipbox.classList.remove('on'),{passive:true});

// --- charts ----------------------------------------------------------------
// Two hues only, and they carry polarity rather than identity: buying vs selling
// is a diverging scale about zero, so aqua/orange with a neutral zero rule is the
// honest encoding. Both steps are the ones publish.py already uses, and the pair
// validates against this surface for contrast and colour-vision separation.
// Chart colours come from the theme tokens, re-read whenever the theme changes.
const C={};
function themeColors(){
  Object.assign(C,{buy:cssVar('--buy'),sell:cssVar('--sell'),ink:cssVar('--ink-2'),
    faint:cssVar('--ink-3'),grid:cssVar('--grid'),surface:cssVar('--panel')});
  Object.assign(tip,{backgroundColor:cssVar('--tip-bg'),borderColor:cssVar('--line-2'),
    titleColor:cssVar('--heading'),bodyColor:cssVar('--ink')});
}
// Same hue, less weight -- for marks that have to sit behind something else.
const fade=(hex,a)=>`rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${
  parseInt(hex.slice(5,7),16)},${a})`;
const charts={};
function paint(id,cfg){
  const el=document.getElementById(id); if(!el) return;
  if(typeof Chart==='undefined'){
    el.closest('.canvas-wrap').innerHTML='<div class="nochart">Charts need Chart.js from '+
      'the CDN, which did not load. The numbers are all in the table below.</div>'; return; }
  if(charts[id]){ charts[id].destroy(); }
  Chart.defaults.color=C.ink; Chart.defaults.font.family=
    "-apple-system,Segoe UI,Roboto,sans-serif"; Chart.defaults.font.size=11.5;
  charts[id]=new Chart(el,cfg);
}
const money=v=>{const a=Math.abs(v);
  return (v<0?'-$':'$')+(a>=1e6?(a/1e6).toFixed(1)+'M':a>=1e3?(a/1e3).toFixed(0)+'k':a);};
// Share prices need the cents that money() throws away, and never the k/M step:
// a $1,200 close is $1,200, not $1k.
const px=v=>'$'+Number(v).toLocaleString(undefined,
  {minimumFractionDigits:v<100?2:0,maximumFractionDigits:2});
// Hairline grid, no border, ticks in muted ink -- chrome stays recessive so the
// bars carry the reading.
const axis=(extra={})=>Object.assign({grid:{color:C.grid,drawBorder:false,drawTicks:false},
  border:{display:false},ticks:{color:C.faint,padding:6}},extra);
const tip={borderWidth:1,padding:9,displayColors:true,boxPadding:4,cornerRadius:6};
themeColors();
addEventListener('themechange',()=>{ themeColors(); route(); });

function tableView(head,rows){
  return '<details class="tv"><summary>Table view</summary><div class="tbl-scroll"><table><thead><tr>'+
    head.map(h=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+r.map((c,i)=>`<td${i?' class="num"':''}>${esc(c)}</td>`).join('')+'</tr>').join('')+
    '</tbody></table></div></details>';
}

// --- trends ----------------------------------------------------------------
const tstate={metric:'net',days:'365',floor:'',chamber:'',unit:'dollars'};
const METRICS=[['net','Net flow'],['volume','Dollar volume'],['count','Trade count'],
  ['members','Distinct members']];

async function trends(){
  app.innerHTML=`
  <div class="filters">
    <div><label>window</label><select id="t-days">${
      [['90','last 90 days'],['180','last 6 months'],['365','last year'],
       ['730','last 2 years'],['0','everything']].map(([v,l])=>
      `<option value="${v}"${tstate.days===v?' selected':''}>${l}</option>`).join('')}</select></div>
    <div><label>chamber</label><select id="t-chamber">${
      ['','House','Senate'].map(c=>`<option value="${c}"${tstate.chamber===c?' selected':''}>${c||'both'}</option>`).join('')}</select></div>
    <div><label>min amount</label><input id="t-floor" inputmode="numeric" placeholder="15001" value="${esc(tstate.floor)}"></div>
  </div>
  <div class="chartbox">
    <h2>Top names</h2>
    <p class="cap" id="t-cap"></p>
    <div class="toggle" id="t-metric">${METRICS.map(([k,l])=>
      `<button data-m="${k}" aria-pressed="${tstate.metric===k}">${l}</button>`).join('')}</div>
    <div class="canvas-wrap" id="w-top"><canvas id="c-top"></canvas></div>
    <div id="t-table"></div>
  </div>
  <div class="chartbox">
    <h2>Buying and selling over time</h2>
    <p class="cap">Disclosures per month, by direction. Amounts are the lower bound of
      each disclosed bracket, so dollar figures are floors rather than exact sums.</p>
    <div class="toggle" id="t-unit">
      <button data-u="dollars" aria-pressed="${tstate.unit==='dollars'}">Dollars</button>
      <button data-u="count" aria-pressed="${tstate.unit==='count'}">Trade count</button></div>
    <div class="canvas-wrap" id="w-time"><canvas id="c-time"></canvas></div>
    <div id="t-time-table"></div>
  </div>`;

  annotate(app.querySelector('.filters'));
  const rerun=()=>{ drawTop(); drawTime(); };
  $('#t-days').onchange=e=>{tstate.days=e.target.value; rerun();};
  $('#t-chamber').onchange=e=>{tstate.chamber=e.target.value; rerun();};
  $('#t-floor').oninput=e=>{tstate.floor=e.target.value; clearTimeout(timer);
    timer=setTimeout(rerun,400);};
  document.querySelectorAll('#t-metric button').forEach(b=>b.onclick=()=>{
    tstate.metric=b.dataset.m;
    document.querySelectorAll('#t-metric button').forEach(x=>
      x.setAttribute('aria-pressed',String(x.dataset.m===tstate.metric)));
    drawTop();});
  document.querySelectorAll('#t-unit button').forEach(b=>b.onclick=()=>{
    tstate.unit=b.dataset.u;
    document.querySelectorAll('#t-unit button').forEach(x=>
      x.setAttribute('aria-pressed',String(x.dataset.u===tstate.unit)));
    drawTime();});
  rerun();
}

function qwin(extra){ return new URLSearchParams(Object.assign(
  {days:tstate.days,chamber:tstate.chamber,floor:tstate.floor},extra||{})); }

async function drawTop(){
  const d=await (await fetch('/api/top?'+qwin({metric:tstate.metric,limit:'18'}))).json();
  const cap=$('#t-cap'), box=$('#t-table');
  if(d.error||!d.rows||!d.rows.length){
    if(cap) cap.textContent='Nothing in this window.';
    $('#w-top').innerHTML='<div class="nochart">No matching trades.</div>';
    if(box) box.innerHTML=''; return; }
  const m=tstate.metric;
  // Colour means direction on every metric here: aqua where the name is net
  // bought over the window, orange where it is net sold. Length still carries the
  // metric the reader picked, so the two channels answer different questions --
  // how much, and which way. Under net flow they agree by construction, because
  // there the bar itself is the signed number.
  const rows=m==='net'?d.rows.slice().sort((a,b)=>b.net-a.net):d.rows;
  const vals=rows.map(r=>m==='members'?r.members:m==='count'?r.count:m==='volume'?r.volume:r.net);
  const dollars=(m==='net'||m==='volume');
  const hue=' Aqua is a name Congress is net buying over this window, orange net selling.';
  cap.textContent=(m==='net'
    ? 'Disclosed buying minus selling per name, ordered by size in either direction, because a name Congress dumped says as much as one it bought.'
    : m==='volume' ? 'Disclosed dollars traded per name, buys and sells together.'
    : m==='count' ? 'Number of disclosures per name.'
    : 'How many different members traded the name -- the convergence signal, hardest to skew with one heavy trader.')+hue;
  const colors=rows.map(r=>r.net>=0?C.buy:C.sell);
  $('#w-top').style.height=Math.max(220,rows.length*26+46)+'px';
  paint('c-top',{type:'bar',data:{labels:rows.map(r=>r.ticker),
      datasets:[{label:METRICS.find(x=>x[0]===m)[1],data:vals,backgroundColor:colors,
        borderWidth:0,borderRadius:4,borderSkipped:'start',barPercentage:.82,
        categoryPercentage:.9}]},
    options:{indexAxis:'y',maintainAspectRatio:false,responsive:true,
      layout:{padding:{right:8}},
      onClick:(ev,els)=>{ if(els&&els.length) tickerPanel(rows[els[0].index].ticker); },
      onHover:(ev,els)=>{ ev.native.target.style.cursor=els.length?'pointer':'default'; },
      plugins:{legend:{display:false},tooltip:Object.assign({},tip,{callbacks:{
        title:tkTitle,
        label:c=>{const r=rows[c.dataIndex];
          // The bar no longer states its own direction once the metric is a
          // magnitude, so the net figure that drives the colour is spelled out.
          const dir=r.net>0?'net buying':r.net<0?'net selling':'balanced';
          return dollars?[`${METRICS.find(x=>x[0]===m)[1]}: ${money(vals[c.dataIndex])}`,
            `bought ${money(r.buy_vol)} in ${r.buys}, sold ${money(r.sell_vol)} in ${r.sells}`,
            `${r.members} member${r.members===1?'':'s'}`,
            ...(m==='net'?[]:[`${dir}: ${money(r.net)}`])]
            :[`${vals[c.dataIndex].toLocaleString()}`,
              `${r.buys} buys, ${r.sells} sells, ${r.members} members`,
              `${dir}: ${money(r.net)}`];}}})},
      scales:{x:axis({ticks:{color:C.faint,padding:6,
          callback:v=>dollars?money(v):v.toLocaleString()},
        grid:{color:C.grid,drawBorder:false,drawTicks:false}}),
        y:axis({grid:{display:false},ticks:{color:C.ink,padding:6,font:{size:11.5}}})}}});
  box.innerHTML=tableView(['ticker','buys','sells','buy $','sell $','net $','members'],
    rows.map(r=>[r.ticker,r.buys,r.sells,money(r.buy_vol),money(r.sell_vol),
      money(r.net),r.members]));
  annotate(document.querySelector('#t-cap')); annotate(box);
}

async function drawTime(){
  const d=await (await fetch('/api/timeline?'+qwin())).json();
  const box=$('#t-time-table');
  if(d.error||!d.rows||!d.rows.length){
    $('#w-time').innerHTML='<div class="nochart">No matching trades.</div>';
    if(box) box.innerHTML=''; return; }
  const r=d.rows, dollars=tstate.unit==='dollars';
  const buy=r.map(x=>dollars?x.buy_vol:x.buys), sell=r.map(x=>dollars?x.sell_vol:x.sells);
  $('#w-time').style.height='300px';
  // Both series are the same measure in the same unit, so they share one axis --
  // a second y-scale here would invent a relationship that is not in the data.
  monthBars('c-time',r,buy,sell,dollars?money:v=>v.toLocaleString(),
    {click:clickMonth(r,d.since,{chamber:tstate.chamber,floor:tstate.floor}),
     callbacks:{afterBody:i=>[`${r[i[0].dataIndex].members} members active`]}});
  box.innerHTML=tableView(['month','buys','sells','buy $','sell $','members'],
    r.map(x=>[x.ym,x.buys,x.sells,money(x.buy_vol),money(x.sell_vol),x.members]));
}

async function tickerPanel(sym){
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const d=await (await fetch('/api/ticker?symbol='+encodeURIComponent(sym))).json();
  const el=document.createElement('div'); el.className='panel';
  if(d.error||!d.agg||!d.agg.n){
    el.innerHTML=`<button class="x" id="closep">close</button><h2>${esc(sym)}</h2>
      <p class="note">${esc(d.error||'No disclosures name this ticker.')}</p>`;
    document.body.appendChild(el); $('#closep').onclick=()=>el.remove(); return; }
  const a=d.agg;
  el.innerHTML=`<button class="x" id="closep">close</button>
    <h2>${esc(d.ticker)}</h2>
    <p class="note">${esc(d.asset_name||'')}${d.sector?' - '+esc(d.sector):''}</p>
    <div class="kv">
      <b>disclosures</b><span>${a.n.toLocaleString()}</span>
      <b>buys / sells</b><span>${a.buys} / ${a.sells}</span>
      <b>net flow</b><span class="${a.net>0?'dir-buy':a.net<0?'dir-sell':''}">${money(a.net)}</span>
      <b>dollar volume</b><span>${money((a.buy_vol||0)+(a.sell_vol||0))}</span>
      <b>distinct members</b><span>${a.members}</span>
      <b>vs index</b><span class="${d.alpha_med>0?'up':d.alpha_med<0?'down':''}">${
        d.alpha_med==null?'not priced yet':pct(d.alpha_med)+' median over '+d.alpha_n+' priced'}</span>
      <b>first seen</b><span>${esc(a.first_seen||'')}</span>
      <b>latest</b><span>${esc(a.last_seen||'')}</span>
    </div>
    ${(d.price&&(d.price.labels||[]).length>2)?`<h3>Price trend</h3>
      <p class="cap" id="px-cap"></p>
      <div class="canvas-wrap" id="w-px" style="height:190px"><canvas id="c-px"></canvas></div>`
      :`<p class="note">No cached closes for this name, so there is no price line.
        <code>prices</code> fetches them on a refresh.</p>`}
    ${d.months.length>1?`<h3>Disclosures per month</h3>
      <div class="canvas-wrap" id="w-tk" style="height:150px"><canvas id="c-tk"></canvas></div>`:''}
    <h3>Who traded it</h3>
    <div class="tbl-scroll"><table><thead><tr><th>member</th><th>buys</th><th>sells</th>
      <th>net</th></tr></thead><tbody>${d.members.map(m=>`<tr>
      <td><span class="mlink ${pc(m.party)}" data-m="${esc(m.member)}">${esc(m.full_name||m.member)}</span></td>
      <td class="num">${m.buys}</td><td class="num">${m.sells}</td>
      <td class="num ${m.net>0?'dir-buy':m.net<0?'dir-sell':''}">${money(m.net)}</td></tr>`).join('')}
      </tbody></table></div>
    <h3>Most recent</h3>
    <div class="tbl-scroll"><table><thead><tr><th>disclosed</th><th>member</th><th>type</th>
      <th>amount</th><th>alpha</th></tr></thead><tbody>${d.recent.map(r=>`<tr>
      <td class="num">${esc(r.disclosed||r.tx_date||'')}</td>
      <td class="${pc(r.party)}">${esc((r.full_name||r.member||'').slice(0,22))}</td>
      <td>${dirTag(r.tx_type)}</td>
      <td class="num">${esc(r.amount_range||'')}</td>
      <td class="${cls(r.alpha)}">${pct(r.alpha)}</td></tr>`).join('')}</tbody></table></div>
    <p style="margin-top:1rem"><button id="onlytk">show only this stock</button>
      <button id="watchtk" data-kind="ticker" data-value="${esc(d.ticker)}"></button></p>`;
  document.body.appendChild(el);
  drawPrice(d);
  if(d.months.length>1){
    paint('c-tk',{type:'bar',data:{labels:d.months.map(m=>m.ym),
        datasets:[{label:'Buys',data:d.months.map(m=>m.buys),backgroundColor:C.buy,
            borderWidth:0,borderRadius:2,borderSkipped:'start',categoryPercentage:.8},
          {label:'Sells',data:d.months.map(m=>m.sells),backgroundColor:C.sell,
            borderWidth:0,borderRadius:2,borderSkipped:'start',categoryPercentage:.8}]},
      options:{maintainAspectRatio:false,responsive:true,
        interaction:{mode:'index',intersect:false},
        plugins:{legend:{display:true,position:'top',align:'end',
            labels:{boxWidth:8,boxHeight:8,usePointStyle:true,pointStyle:'rect',padding:10}},
          tooltip:tip},
        scales:{x:axis({grid:{display:false},ticks:{color:C.faint,maxTicksLimit:6}}),
          y:axis({beginAtZero:true,ticks:{color:C.faint,precision:0}})}}});
  }
  el.querySelectorAll('.mlink').forEach(e=>e.onclick=()=>member(e.dataset.m));
  $('#closep').onclick=()=>el.remove();
  $('#onlytk').onclick=()=>{ st.ticker=d.ticker; st.member=''; st.q=''; st.offset=0;
    el.remove(); if(location.hash!=='#/explore') location.hash='#/explore'; else explore(); };
  watchButton($('#watchtk'));
  annotate(el);
}

// --- watchlist ---------------------------------------------------------------
// Members and tickers you follow. Saved in the database on this machine; every
// trade they touch alerts and lands in the digest.
let WATCH=null;
async function loadWatch(){ WATCH=await (await fetch('/api/watch')).json(); return WATCH; }
const watching=(kind,v)=>!!(WATCH&&WATCH[kind].some(x=>x.value===v));
async function watchSet(kind,value,on){
  const r=await (await fetch('/api/watch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind,value,on})})).json();
  if(!r.error) WATCH=r;
  return r;
}
async function watchButton(b){
  if(!b) return;
  if(READ_ONLY){ b.remove(); return; }
  if(!WATCH) await loadWatch();
  const draw=()=>{ const on=watching(b.dataset.kind,b.dataset.value);
    b.textContent=on?'unwatch':'watch'; b.setAttribute('aria-pressed',on); };
  draw();
  b.onclick=async()=>{ b.disabled=true;
    const r=await watchSet(b.dataset.kind,b.dataset.value,!watching(b.dataset.kind,b.dataset.value));
    if(r.error) alert(r.error);
    b.disabled=false; draw(); };
}
async function watchPage(){
  app.innerHTML='<div class="spin">loading...</div>';
  const w=await loadWatch();
  if(!facets.members.length) facets=await (await fetch('/api/facets')).json();
  const list=(kind,label)=>`<div class="chartbox"><h2>${label}</h2>${
    w[kind].length?`<div class="tbl-scroll"><table><thead><tr><th>${kind}</th><th>name</th>
      <th class="num">trades, 90 days</th><th></th></tr></thead><tbody>${w[kind].map(x=>`<tr>
      <td>${kind==='ticker'?`<b class="mlink" data-t="${esc(x.value)}">${esc(x.value)}</b>`
        :`<span class="mlink" data-m="${esc(x.value)}">${esc(x.value)}</span>`}</td>
      <td>${esc(x.name)}</td><td class="num">${x.n90}</td>
      <td><button data-rm-kind="${kind}" data-rm="${esc(x.value)}">remove</button></td></tr>`).join('')}
      </tbody></table></div>`:'<p class="note">Nothing yet.</p>'}</div>`;
  app.innerHTML=`<div class="chartbox"><h2>Add to your watchlist</h2>
      <p class="cap">Every trade by these members, or in these tickers, raises an alert at
      any size and appears in the digest. Tickers need not have been traded yet: list what
      you own and you will hear if Congress trades it. Saved on this machine only.</p>
      <div class="filters">
        <div><label>watch</label><select id="w-kind"><option value="ticker">tickers</option>
          <option value="member">a member</option></select></div>
        <div style="grid-column:span 2"><label>value</label><input id="w-val" list="w-opts"
          placeholder="AAPL MSFT NVDA" autocomplete="off"><datalist id="w-opts"></datalist></div>
        <div class="chk"><button id="w-add">Add</button></div>
      </div><p class="note" id="w-msg"></p></div>
    ${list('member','Members')}${list('ticker','Tickers')}
    <p><button id="w-explore">show their trades in Explore</button></p>`;
  const kind=$('#w-kind'), val=$('#w-val'), msg=$('#w-msg');
  const opts=()=>{ $('#w-opts').innerHTML=(kind.value==='member'
      ?facets.members.map(m=>m.full_name||m.member):facets.tickers.map(t=>t.ticker))
      .map(v=>`<option value="${esc(v)}">`).join('');
    val.placeholder=kind.value==='member'?'any part of a name, e.g. Pelosi':'AAPL MSFT NVDA'; };
  kind.onchange=opts; opts();
  const add=async()=>{ if(!val.value.trim()) return;
    const r=await watchSet(kind.value,val.value.trim(),true);
    if(r.error){ msg.textContent=r.error; msg.className='note bad'; return; }
    watchPage(); };
  $('#w-add').onclick=add; val.onkeydown=e=>{ if(e.key==='Enter') add(); };
  app.querySelectorAll('button[data-rm]').forEach(b=>b.onclick=async()=>{
    await watchSet(b.dataset.rmKind,b.dataset.rm,false); watchPage(); });
  app.querySelectorAll('.mlink').forEach(e=>e.onclick=()=>
    e.dataset.t?tickerPanel(e.dataset.t):member(e.dataset.m));
  $('#w-explore').onclick=()=>{ Object.assign(st,{q:'',member:'',ticker:'',chamber:'',type:'',
    floor:'',since:'',until:'',tickered:'0',watched:'1',offset:0});
    location.hash='#/explore'; };
}

// --- reports ---------------------------------------------------------------
const ropts={};
async function report(name){
  const spec=REPORTS[name]; if(!spec){ app.innerHTML='<div class="err">No such report.</div>'; return; }
  const o=ropts[name]=ropts[name]||{};
  const ctl=Object.entries(spec.args).map(([k,t])=>{
    if(t==='bool') return `<div class="chk"><input type="checkbox" id="o-${k}"${o[k]?' checked':''}>
      <label style="margin:0;text-transform:none;font-size:13px">${esc(k.replace('-',' '))}</label></div>`;
    const dflt=(spec.defaults||{})[k];
    if(k==='horizon'){ const cur=o[k]||dflt||'90';
      return `<div><label>${k}</label><select id="o-${k}">${
      ['30','90'].map(v=>`<option${cur===v?' selected':''}>${v}</option>`).join('')}</select></div>`; }
    return `<div><label>${esc(k.replace('-',' '))}</label><input id="o-${k}" inputmode="numeric"
      placeholder="${esc(dflt==null?'':dflt)}" value="${esc(o[k]==null?'':o[k])}"></div>`;
  }).join('');
  app.innerHTML=(ctl?`<div class="filters">${ctl}</div>`:'')+'<div id="pcall"></div>'+'<div id="out"><div class="spin">running...</div></div>';
  if(['scorecard','backtest'].includes(name)) fetch('/api/stats').then(r=>r.json()).then(s=>{
    const el=$('#pcall'); if(el) el.innerHTML=persistenceCallout(s.persistence); });
  Object.keys(spec.args).forEach(k=>{ const el=$('#o-'+k); if(!el) return;
    el.addEventListener(el.type==='checkbox'||el.tagName==='SELECT'?'change':'input',()=>{
      o[k]=el.type==='checkbox'?(el.checked?'1':''):el.value;
      clearTimeout(timer); timer=setTimeout(()=>run(name),el.tagName==='INPUT'&&el.type!=='checkbox'?400:0);});});
  run(name);
}
async function run(name){
  const out=$('#out'); if(!out) return;
  const p=new URLSearchParams(Object.entries(ropts[name]||{}).filter(([,v])=>v!==''&&v!=null));
  out.innerHTML='<div class="spin">running...</div>';
  const d=await (await fetch(`/api/report/${name}?`+p)).json();
  out.innerHTML=(d.refreshing?'<p class="note">A refresh is running; these are the '+
    'pre-refresh numbers until it commits.</p>':'')+
    (d.rc?`<div class="err">Exited ${d.rc}.</div>`:'')+d.html;
  annotate(out);
}

// --- maintenance: the jobs that write, and the model they need --------------
async function jobs(){
  app.innerHTML='<div class="spin">loading...</div>';
  const d=await (await fetch('/api/jobs')).json();
  const p=d.prereq;
  const row=(k,label)=>`<div><b>${esc(label)}</b> <span class="${p[k].ok?'num ok':'num bad'}">`+
    `${p[k].ok?'ok':'not ready'}</span> &mdash; ${esc(p[k].detail)}`+
    (!p[k].ok&&p[k].url?` &mdash; <a href="${esc(p[k].url)}" target="_blank" rel="noopener">`+
      `get one free</a>, then add <code>CONGRESS_API_KEY=...</code> to <code>.env</code>`:'')+`</div>`;
  const cards=d.jobs.map(j=>{
    const dis=(!j.ready||d.running)?' disabled':'';
    const since=j.since?`<input id="since" value="2025-01-01" inputmode="numeric"
      style="max-width:9rem;margin-right:.5rem" aria-label="fetch from this date">`:'';
    const why=!j.ready?`<p class="note neg">${esc(j.why)}</p>`
      :(d.running?'<p class="note">Another job is running; only one writes at a time.</p>':'');
    return `<div class="chartbox"><h2>${esc(j.title)}</h2>
      <p class="cap">${esc(j.blurb)}</p>${why}
      <div>${since}<button data-job="${esc(j.name)}"${dis}>${esc(j.title)}</button></div></div>`;
  }).join('');
  app.innerHTML=`<div class="chartbox"><h2>Model</h2>
      <p class="cap">What <code>advise</code>, <code>resolve</code> and <code>topics</code>
      will use. Saved to <code>.env</code> and used by the next job you start here.</p>
      <div class="filters" id="mform">
        <div><label>provider</label><select id="m-preset"></select></div>
        <div id="m-basebox"><label>endpoint</label><input id="m-base"></div>
        <div id="m-keybox"><label>API key</label><input id="m-key" type="password"
          autocomplete="off" spellcheck="false"></div>
        <div><label>model</label><input id="m-model" list="m-models" autocomplete="off"
          spellcheck="false"><datalist id="m-models"></datalist></div>
        <div class="chk"><button id="m-list">List models</button>
          <button id="m-save">Save</button></div>
      </div><p class="note" id="m-msg"></p>
      ${row('model','Model')}${row('key','Congress.gov key')}${row('titles','Meeting titles')}
      <p class="note">The check below is a live round trip. Its last step asks for JSON
      constrained by a schema &mdash; the part <code>resolve</code> and <code>topics</code>
      rest on, and the part an endpoint can silently ignore while still answering.</p>
      <div><button id="llmck">Check model</button></div>
      <pre id="llmout" style="display:none"></pre></div>
    <h2>Jobs</h2>
    <p class="note">Each of these writes to the database, so only one runs at a time and
    they share the slot with a refresh. Progress shows in the bar at the top of the page.</p>
    ${cards}`;
  modelForm();
  $('#llmck').onclick=async e=>{
    const b=e.target, out=$('#llmout');
    b.disabled=true; b.textContent='checking...';
    out.style.display=''; out.textContent='running a live round trip...';
    try{ const r=await (await fetch('/api/llm')).json();
      out.textContent=r.text||'(no output)';
      out.className=r.rc?'err':'';
    }catch(err){ out.textContent='could not reach the server'; }
    b.disabled=false; b.textContent='Check model';
  };
  app.querySelectorAll('button[data-job]').forEach(b=>{ b.onclick=async()=>{
    const since=$('#since')?('?since='+encodeURIComponent($('#since').value.trim())):'';
    b.disabled=true;
    const r=await (await fetch('/job/'+b.dataset.job+since,{method:'POST'})).json();
    if(!r.started){ alert(r.why||'could not start'); b.disabled=false; return; }
    tick(); jobs();
  };});
}

// --- model picker -----------------------------------------------------------
async function modelForm(){
  const {presets,current:cur}=await (await fetch('/api/model')).json();
  const P=Object.fromEntries(presets.map(p=>[p.id,p]));
  const sel=$('#m-preset'), base=$('#m-base'), key=$('#m-key'), model=$('#m-model'),
    dl=$('#m-models'), msg=$('#m-msg');
  if(!sel) return;
  sel.innerHTML=presets.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
  const say=(t,bad)=>{msg.textContent=t; msg.className='note'+(bad?' bad':'');};
  const fill=list=>{dl.innerHTML=(list||[]).map(m=>`<option value="${esc(m)}">`).join('');};
  // Same-provider fields keep what is saved; switching provider starts clean,
  // and the saved key is never offered to a different company.
  const show=keep=>{
    const p=P[sel.value], same=keep&&sel.value===cur.preset;
    base.value=same?cur.base:p.base; base.readOnly=!p.editable;
    $('#m-basebox').style.opacity=p.editable?1:.6;
    model.value=same?cur.model:''; key.value=''; fill(p.models);
    key.placeholder=p.provider==='anthropic'&&cur.login?`blank: signed in (${cur.login})`
      :same&&cur.key_set?`saved${cur.key_tail?' (...'+cur.key_tail+')':''} - blank keeps it`
      :p.provider==='anthropic'?'API key, or run: ant auth login':(p.key?'required':'optional');
    $('#m-keybox').style.display=p.provider==='ollama'?'none':'';
    model.placeholder=(p.models&&p.models[0])||'List models, or type one';
    say('');
  };
  sel.value=cur.preset; show(true);
  sel.onchange=()=>show(true);
  const body=()=>JSON.stringify({preset:sel.value,base:base.value.trim(),
    key:key.value.trim(),model:model.value.trim()});
  const post=async url=>{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body()});
    return r.json();};
  $('#m-list').onclick=async e=>{
    e.target.disabled=true; say('asking the provider...');
    try{ const r=await post('/api/models');
      if(r.error) say(r.error,true);
      else{ fill([...new Set([...(P[sel.value].models||[]),...r.models])]);
        say(`${r.models.length} models available - pick one in the model box.`); model.focus(); }
    }catch(err){ say('could not reach the portal',true); }
    e.target.disabled=false;
  };
  $('#m-save').onclick=async e=>{
    e.target.disabled=true;
    try{ const r=await post('/api/model');
      if(r.error){ say(r.error,true); e.target.disabled=false; return; }
      await jobs(); const m=$('#m-msg');
      if(m){ m.textContent='Saved. Press Check model to try it.'; m.className='note ok'; }
    }catch(err){ say('could not reach the portal',true); e.target.disabled=false; }
  };
}

function full(){
  app.innerHTML='<p class="note">The rendered static page, exactly as `publish` writes it. '+
    '<a href="/page" target="_blank" rel="noopener">Open in its own tab</a>.</p>'+
    '<iframe src="/page" title="rendered page"></iframe>';
}

// --- refresh + live stats ---------------------------------------------------
async function tick(){
  try{
    const s=await (await fetch('/status')).json();
    const b=$('#rf'), l=$('#live');
    if(s.running){ b.disabled=true; b.textContent=(s.verb||'Running')+'...';
      l.textContent=(s.line||'')+(s.elapsed?`  (${Math.floor(s.elapsed/60)}m${s.elapsed%60}s)`:'');
      setTimeout(tick,1500);
    } else {
      b.disabled=false; b.textContent='Refresh data';
      if(s.rc===0){ l.textContent=(s.title||'done')+' finished'; statline(); route();
        setTimeout(()=>l.textContent='',4000); }
      else if(s.rc!=null){ l.textContent=(s.title||'job')+' failed: '+(s.line||'see the terminal'); }
    }
  }catch(e){ $('#live').textContent='lost contact with the server'; }
}
async function statline(){
  try{ const s=await (await fetch('/api/stats')).json();
    if(s&&s.trades) $('#stat').textContent=
      `${s.trades.toLocaleString()} trades - ${s.members} members - ${s.priced.toLocaleString()} priced`;
    // Warn before a refresh dies at `sectors` rather than after.
    const old=$('#nocontact'); if(old) old.remove();
    if(s&&!s.contact){ const w=document.createElement('div'); w.className='err'; w.id='nocontact';
      w.innerHTML='<b>CONGRESS_CONTACT is not set.</b> Collection will stop at '+
        '<code>sectors</code>: the SEC answers 403 to anonymous clients. Start the portal '+
        'with <code>./run.sh portal</code>, or export it before launching.';
      app.parentNode.insertBefore(w,app); }
  }catch(e){}
}
$('#rf').onclick=async()=>{ $('#rf').disabled=true; $('#live').textContent='starting...';
  await fetch('/refresh',{method:'POST'}); tick(); };

$('#st').onclick=async()=>{
  if(!confirm('Stop the portal? The page stays open but stops working until you '+
              'restart it with ./run.sh portal')) return;
  let r=await fetch('/shutdown',{method:'POST'});
  if(r.status===409){
    const d=await r.json();
    if(!confirm((d.note||'A refresh is running.')+'\n\nStop anyway?')) return;
    r=await fetch('/shutdown?force=1',{method:'POST'});
  }
  $('#st').disabled=true; $('#rf').disabled=true;
  $('#live').textContent='portal stopped - restart with ./run.sh portal';
};

// Hidden rather than removed: tick() and the handlers above still address them.
if(READ_ONLY) ['#rf','#st'].forEach(s=>$(s).hidden=true);
wireThemeToggle($('#theme'));
statline(); route(); tick();
