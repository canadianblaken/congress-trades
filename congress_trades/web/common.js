// Shared by the portal and the static page, so the two cannot drift on these.
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// A member's name in their party's colour: "D" or "Democrat" -> pD; unknown -> ''.
const pc=p=>({D:'pD',R:'pR',I:'pI'})[(p||'')[0]]||'';
