let chatBusy=false;
function chatBubble(text,kind='assistant',records=[],scope=''){
  const item=document.createElement('div');item.className='chat-bubble '+kind;
  if(scope){const label=document.createElement('small');label.className='chat-answer-scope';label.textContent=scope;item.append(label);}
  const body=document.createElement('div');body.textContent=text;item.append(body);
  for(const record of records){const link=document.createElement('button');link.className='chat-record';link.textContent=record.label+(record.environment?' · '+record.environment:'')+' ↗';link.type='button';link.onclick=()=>{if(scope&&scope!==state.data?.release.name){toast('Select the release shown above this answer to inspect its record.');return;}const exists=(record.type==='activity'?state.data.activities:state.data.defects).some(r=>r.id===record.id);if(!exists){toast('Refresh this release to load the source record.');return;}if(record.type==='activity')showActivity(record.id);else showDefect(record.id);};item.append(link);}
  $('#chat-history').append(item);$('#chat-history').scrollTop=$('#chat-history').scrollHeight;return item;
}
function showChat(open){$('#release-chat').hidden=!open;$('#chat-toggle').setAttribute('aria-expanded',String(open));if(open){$('#chat-scope').textContent='Selected release: '+(state.data?.release.name||'Choose a release first');if(!$('#chat-history').children.length)chatBubble('Ask me about a defect, a service task, or incomplete activities. I’ll show the records behind the answer.');$('#chat-question').focus();}else $('#chat-toggle').focus();}
$('#chat-toggle').onclick=()=>showChat($('#release-chat').hidden);
$('#chat-close').onclick=()=>showChat(false);
$('#release-chat').addEventListener('keydown',event=>{if(event.key==='Escape'){showChat(false);event.stopPropagation();}});
$('#release-picker').addEventListener('change',()=>{$('#chat-scope').textContent='Selected release: '+$('#release-picker').selectedOptions[0].textContent;});
async function sendChat(question){
  if(chatBusy||!question.trim())return;
  if(!state.releaseId){toast('Select a release before asking a question.');return;}
  const releaseId=state.releaseId,scope=state.data.release.name;
  chatBusy=true;$('#chat-send').disabled=true;$('#chat-question').value='';chatBubble(question,'user',[],scope);const pending=chatBubble('Checking release records…');
  try{const answer=await api('/api/chat','POST',{release_id:releaseId,question});pending.remove();chatBubble(answer.answer,'assistant',answer.records,answer.release_name);}
  catch(error){pending.remove();chatBubble('I could not retrieve the records. '+error.message);}
  finally{chatBusy=false;$('#chat-send').disabled=false;$('#chat-question').focus();}
}
$('#chat-form').onsubmit=event=>{event.preventDefault();sendChat($('#chat-question').value.trim());};
document.querySelectorAll('[data-question]').forEach(button=>button.onclick=()=>sendChat(button.dataset.question));
