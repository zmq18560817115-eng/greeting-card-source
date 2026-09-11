/* Shared selection gestures for filtered tables. No requests or record edits. */
(function(root){
  function range(base,ids,start,end,checked){
    const visible=new Set(ids),next=new Set([...base].filter(id=>visible.has(id)));
    if(start<0||end<0||start>=ids.length||end>=ids.length)return next;
    for(let i=Math.min(start,end);i<=Math.max(start,end);i++)checked?next.add(ids[i]):next.delete(ids[i]);
    return next;
  }
  function edgeSpeed(y,top,bottom){
    const edge=Math.min(48,(bottom-top)/3);
    if(edge<=0)return 0;
    if(y<top+edge)return -900*Math.min(1,(top+edge-y)/edge);
    if(y>bottom-edge)return 900*Math.min(1,(y-bottom+edge)/edge);
    return 0;
  }
  const controllers=new WeakMap();let active=null;
  function attach({container,selector,allId,idOf,ids,selected,change,locked}){
    if(controllers.has(container))return;
    let anchor=null,gesture=null,frame=0,suppressClick=false,order='';
    const inputs=()=>Array.from(container.querySelectorAll(selector));
    function paint(){
      const values=selected();
      for(const input of inputs()){
        const checked=values.has(idOf(input));input.checked=checked;
        const row=input.closest('tr');row.classList.toggle('is-selected',checked);row.setAttribute('aria-selected',String(checked));
      }
      const all=container.querySelector('#'+allId),visible=ids(),count=visible.filter(id=>values.has(id)).length;
      if(all){all.checked=visible.length>0&&count===visible.length;all.indeterminate=count>0&&count<visible.length;}
    }
    function apply(values){change(values);paint();}
    function refresh(){
      finish(false);
      const current=JSON.stringify(ids());if(order!==current||!selected().size)anchor=null;order=current;
      paint();
    }
    function finish(restore){
      if(!gesture)return;
      const previous=gesture;gesture=null;cancelAnimationFrame(frame);
      if(restore)apply(new Set([...previous.base].filter(id=>ids().includes(id))));
      container.classList.remove('is-range-selecting');document.body.classList.remove('is-selecting-rows');
      if(active===controller)active=null;
      try{if(container.hasPointerCapture(previous.pointerId))container.releasePointerCapture(previous.pointerId);}catch{}
    }
    function scrollParent(input){
      for(let node=input.parentElement;node&&node!==document.body;node=node.parentElement){
        if(/auto|scroll/.test(getComputedStyle(node).overflowY)&&node.scrollHeight>node.clientHeight+2)return node;
      }
      return document.scrollingElement;
    }
    function endpoint(y){
      const rows=gesture.rows;let low=0,high=rows.length-1;
      while(low<high){const middle=Math.floor((low+high)/2);if(y>=rows[middle].getBoundingClientRect().bottom)low=middle+1;else high=middle;}
      return low;
    }
    function extend(y){
      if(!gesture)return;
      if(locked()||!gesture.rows[0]?.isConnected){finish(false);return;}
      const end=endpoint(y);if(end===gesture.end)return;gesture.end=end;
      apply(range(gesture.base,gesture.ids,gesture.start,end,gesture.checked));
    }
    function tick(time){
      if(!gesture)return;
      const scroller=gesture.scroller,view=scroller===document.scrollingElement?{top:0,bottom:innerHeight}:scroller.getBoundingClientRect();
      const table=container.getBoundingClientRect(),top=Math.max(0,view.top,table.top),bottom=Math.min(innerHeight,view.bottom,table.bottom);
      const elapsed=Math.min(32,time-gesture.time||16);gesture.time=time;
      if(gesture.moved)scroller.scrollTop+=edgeSpeed(gesture.y,top,bottom)*elapsed/1000;
      extend(gesture.y);if(gesture)frame=requestAnimationFrame(tick);
    }
    function checkbox(target){
      if(!(target instanceof Element))return null;
      const exact=target.closest(selector);if(exact&&container.contains(exact))return exact;
      const cell=target.closest('td.selection-cell');return cell&&container.contains(cell)?cell.querySelector(selector):null;
    }
    container.addEventListener('pointerdown',event=>{
      suppressClick=false;
      if(event.button!==0||event.isPrimary===false||locked())return;
      const input=checkbox(event.target);if(!input||input.disabled)return;
      const visible=ids(),index=visible.indexOf(idOf(input));if(index<0)return;
      if(active)active.finish(false);
      const boxes=inputs();if(boxes.length!==visible.length)return;
      event.preventDefault();input.focus({preventScroll:true});
      const start=event.shiftKey&&visible.includes(anchor)?visible.indexOf(anchor):index;
      if(!event.shiftKey||!visible.includes(anchor))anchor=visible[index];
      gesture={pointerId:event.pointerId,base:new Set(selected()),ids:visible,rows:boxes.map(box=>box.closest('tr')),
        start,end:index,checked:!selected().has(visible[index]),y:event.clientY,originY:event.clientY,originX:event.clientX,
        moved:false,time:0,scroller:scrollParent(input)};
      active=controller;suppressClick=true;
      container.classList.add('is-range-selecting');document.body.classList.add('is-selecting-rows');
      apply(range(gesture.base,visible,start,index,gesture.checked));
      try{container.setPointerCapture(event.pointerId);}catch{}
      frame=requestAnimationFrame(tick);
    });
    document.addEventListener('pointermove',event=>{
      if(!gesture||event.pointerId!==gesture.pointerId)return;
      if(event.pointerType==='mouse'&&!(event.buttons&1)){finish(false);return;}
      event.preventDefault();gesture.y=event.clientY;
      if(Math.hypot(event.clientY-gesture.originY,event.clientX-gesture.originX)>4)gesture.moved=true;
      extend(event.clientY);
    },{passive:false});
    document.addEventListener('pointerup',event=>{if(gesture&&event.pointerId===gesture.pointerId){extend(event.clientY);finish(false);}});
    document.addEventListener('pointercancel',event=>{if(gesture&&event.pointerId===gesture.pointerId)finish(true);});
    container.addEventListener('lostpointercapture',()=>finish(false));
    container.addEventListener('click',event=>{
      if(suppressClick&&event.detail>0){suppressClick=false;event.preventDefault();event.stopImmediatePropagation();queueMicrotask(paint);}
    },true);
    container.addEventListener('change',event=>{
      const input=event.target;if(locked()||input.disabled){paint();return;}
      const visible=ids();
      if(input.id===allId){anchor=null;apply(input.checked?new Set(visible):new Set());}
      else if(input.matches(selector)){
        const id=idOf(input),next=new Set(selected());input.checked?next.add(id):next.delete(id);anchor=id;apply(next);
      }
    });
    container.addEventListener('keydown',event=>{
      if(event.key!==' '||!event.shiftKey||locked())return;
      const input=event.target;if(!input.matches(selector)||input.disabled)return;
      event.preventDefault();const visible=ids(),end=visible.indexOf(idOf(input));
      const start=visible.includes(anchor)?visible.indexOf(anchor):end;
      apply(range(selected(),visible,start,end,!input.checked));if(anchor===null)anchor=idOf(input);
    });
    document.addEventListener('keydown',event=>{if(gesture&&event.key==='Escape'){event.preventDefault();finish(true);}});
    window.addEventListener('blur',()=>finish(false));
    const controller={refresh,finish,dragging:()=>Boolean(gesture)};controllers.set(container,controller);refresh();
  }
  root.RowSelection={range,edgeSpeed,attach,refresh:container=>controllers.get(container)?.refresh(),
    isDragging:container=>Boolean(controllers.get(container)?.dragging())};
  if(typeof module!=='undefined')module.exports=root.RowSelection;
})(globalThis);
