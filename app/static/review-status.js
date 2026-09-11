/* Three presentation groups; delivery actions still use the original workflow state. */
(function(root){
  const labels=Object.freeze({pending:'待推送',sent:'已推送',failed:'推送失败'});
  function of(event){
    // An uncertain receipt or dry run is never proof of successful delivery.
    if(['delivery_unknown','simulated'].includes(event.status)||['unknown','simulated'].includes(event.delivery_state))return 'pending';
    if(event.pushed_at||event.status==='pushed'||event.delivery_state==='sent')return 'sent';
    if(['failed','gen_failed','blocked','expired'].includes(event.status))return 'failed';
    return 'pending';
  }
  function note(event){
    return event.exception_hint||({
      generating:'正在生成海报，完成后自动排期，可抽查。',
      pushing:'正在发送，请等待结果，不要重复操作。',
      simulated:'演练完成，未向飞书发送消息。',
      skipped:'已跳过本次，不会自动推送。',
      needs_regeneration:'资料已修改，系统将重新生成有效日期内的海报。',
      delivery_unknown:'发送结果尚未确认，员工可能已收到；请到飞书核实，暂勿重发。',
      gen_failed:'海报生成失败，请检查模板和底图。',
      blocked:'飞书对应异常，请查看原因并处理。',
      expired:'发送日期已过期，系统已停止推送。',
      failed:'推送失败，请查看具体原因。'
    }[event.status]||'');
  }
  root.ReviewStatus={labels,of,note};
  if(typeof module!=='undefined')module.exports=root.ReviewStatus;
})(globalThis);
