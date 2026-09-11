'use strict';
const poster=document.querySelector('#poster'),fit=document.querySelector('#fit');
poster.src=location.pathname.replace(/\/$/,'')+'/poster';
poster.onerror=()=>{document.querySelector('#error').hidden=false;poster.hidden=true;};
function toggle(){document.body.classList.toggle('original');fit.textContent=document.body.classList.contains('original')?'适应窗口':'查看原图';}
fit.onclick=toggle;poster.onclick=toggle;
document.querySelector('#close').onclick=()=>{window.close();if(history.length>1)history.back();};
