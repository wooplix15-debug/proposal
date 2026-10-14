/* Separate Stitch UI workflow; original document handlers stay in place. */
(() => {
  const panel=document.querySelector('#stitchPanel');
  const card=document.querySelector('#documentCard');
  const form=document.querySelector('#stitchForm');
  const message=document.querySelector('#stitchStatus');
  const previews=document.querySelector('#stitchPreviews');
  const generate=document.querySelector('#stitchGenerate');
  const check=document.querySelector('#stitchCheck');
  const download=document.querySelector('#stitchDownload');
  let designBusy=false, token=sessionStorage.getItem('wooplix-stitch-token')||'';
  let started=Boolean(token);
  const urls=[];
  check.hidden=!token;
  async function request(endpoint,data) {
    const response=await fetch('/api/ui-design/'+endpoint,{method:'POST',body:data,credentials:'same-origin'});
    if(!response.ok) {
      let value;try {value=await response.json();}catch {throw new Error('The design request timed out. Use Check status before starting again.');}
      const detail=value.detail;
      if(detail?.project_token)setToken(detail.project_token);
      throw new Error(typeof detail==='string'?detail:detail?.message||'The design request failed.');
    }
    return response;
  }
  function tokenData() {const data=new FormData();data.append('project_token',token);return data;}
  function setToken(value) {token=value;sessionStorage.setItem('wooplix-stitch-token',value);check.hidden=false;}
  function display(result) {
    if(result.project_token)setToken(result.project_token);
    if(result.status!=='ready') {message.textContent=result.provider_message||result.message||'Stitch is still generating. Check status again.';return false;}
    previews.replaceChildren();
    result.screens.forEach((screen,index)=> {
      const section=document.createElement('section');
      const heading=document.createElement('h3');heading.textContent=screen.title;section.append(heading);
      const frame=document.createElement('iframe');frame.title=screen.title;frame.sandbox='allow-scripts';frame.referrerPolicy='no-referrer';frame.srcdoc=screen.html;
      frame.style.cssText='width:100%;height:720px;border:1px solid #dbe2ed;border-radius:8px;background:white';section.append(frame);
      if(screen.screenshot) {
        const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='View original Stitch screenshot';details.append(summary);
        const image=document.createElement('img');image.alt=screen.title+' original screenshot';image.src=screen.screenshot;image.style.width='100%';details.append(image);section.append(details);
      }
      const note=document.createElement('p');note.textContent=screen.offline_complete?'HTML and screenshot assets are bundled for offline viewing.':'Offline screenshot available. HTML export notes: '+screen.warnings.join(' ');section.append(note);previews.append(section);
    });
    download.hidden=false;message.textContent=`Design ready: ${result.screens.length} page${result.screens.length===1?'':'s'} generated. Review them below and download the HTML and screenshots. This is a visual prototype.`;
    return true;
  }
  function lock(value) {designBusy=value;generate.disabled=value||started;check.disabled=value;download.disabled=value;}
  async function status() {
    if(designBusy||!token)return;
    lock(true);message.textContent='Checking Stitch for finished screens…';
    try {display(await (await request('status',tokenData())).json());}
    catch(error){message.textContent=error.message;}finally{lock(false);}
  }
  document.querySelectorAll('.document-option').forEach(button=> {
    const original=button.onclick;
    button.onclick=async event=> {
      // `busy` belongs to the inline app script's closure and is not visible here.
      // Calling the original handler lets it enforce its own busy guard.
      if(designBusy)return;
      original.call(button,event);
      const selected=button.dataset.document==='ui_design';panel.hidden=!selected;card.hidden=selected;
      if(selected) {
        document.querySelector('#documentBadge').textContent='Stitch UI design workspace';
        document.querySelector('#documentTitle').textContent='Generate your UI design.';
        document.querySelector('#documentDescription').textContent='Upload an approved BRD or client requirements and generate all required pages with Google Stitch.';
        document.title='Wooplix UI Design';
        try {
          const response=await fetch('/api/ui-design/config',{credentials:'same-origin'});const config=await response.json();
          if(response.ok&&!config.configured)message.textContent='Stitch is not configured. Add STITCH_API_KEY to the server environment.';
        }catch {message.textContent='Could not check Stitch configuration.';}
      }
    };
  });
  form.onsubmit=async event=> {
    event.preventDefault();if(designBusy||started)return;
    const data=new FormData(form);
    if(!data.get('text')?.trim()&&!data.get('files')?.size){message.textContent='Upload your BRD or paste its requirements.';return;}
    lock(true);previews.replaceChildren();download.hidden=true;message.textContent='Connecting to Stitch…';
    try {
      const prepared=await (await request('prepare',data)).json();setToken(prepared.project_token);started=true;lock(true);
      message.textContent='Stitch is generating the pages required by your brief. This may take a few minutes. Keep this page open.';
      display(await (await request('generate',tokenData())).json());
    }catch(error){message.textContent=error.message;}
    finally{lock(false);}
  };
  check.onclick=status;
  document.querySelector('#stitchReset').onclick=()=> {
    if(designBusy)return;
    started=false;token='';sessionStorage.removeItem('wooplix-stitch-token');check.hidden=true;download.hidden=true;previews.replaceChildren();message.textContent='Ready for a new screen.';lock(false);
  };
  download.onclick=async()=> {
    if(designBusy||!token)return;lock(true);
    try {
      const response=await request('download',tokenData());const url=URL.createObjectURL(await response.blob());urls.push(url);
      const link=document.createElement('a');link.href=url;link.download='Wooplix_UI_Designs.zip';document.body.append(link);link.click();link.remove();
      message.textContent='Downloaded. Extract the ZIP and open index.html locally. Check manifest.json for offline asset notes.';
    }catch(error){message.textContent=error.message;}finally{lock(false);}
  };
  window.addEventListener('beforeunload',()=>urls.forEach(url=>URL.revokeObjectURL(url)));
  lock(false);
})();
