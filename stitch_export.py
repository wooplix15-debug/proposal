"""Download Stitch artifacts and produce a sandboxed, offline design export."""
import base64
import hashlib
import html
import io
import json
import re
from urllib.parse import urljoin, urlparse
import zipfile

from bs4 import BeautifulSoup
import httpx
import json5
from stitch_client import StitchError

ALLOWED_HOSTS = ('googleusercontent.com', 'gstatic.com', 'storage.googleapis.com',
                 'fonts.googleapis.com', 'contribution.usercontent.google.com', 'cdn.tailwindcss.com', 'images.unsplash.com', 'placehold.co')
MAX_BYTES = 12 * 1024 * 1024
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"

def allowed(url):
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return False
    return p.scheme == 'https' and not p.username and not p.password and port in (None,443) and any(
        p.hostname == h or (h in ('googleusercontent.com','gstatic.com') and (p.hostname or '').endswith('.'+h)) for h in ALLOWED_HOSTS)

class Assets:
    def __init__(self):
        self.cache, self.total = {}, 0
    async def fetch(self, url):
        if url in self.cache: return self.cache[url]
        original=url
        # Read-only asset downloads can be retried; generation must not be.
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
                    current=original
                    for _ in range(4):
                        if not allowed(current): raise StitchError('A design asset uses an unsupported host.')
                        async with client.stream('GET',current) as response:
                            if response.is_redirect:
                                current=urljoin(current,response.headers.get('location','')); continue
                            response.raise_for_status()
                            data=b''
                            async for chunk in response.aiter_bytes():
                                data+=chunk
                                if len(data)+self.total > MAX_BYTES: raise StitchError('Design assets exceed the 12 MB export limit.')
                            self.total+=len(data)
                            result=(data,response.headers.get('content-type','application/octet-stream').split(';')[0])
                            self.cache[original]=result
                            return result
                raise StitchError('Too many redirects while downloading a design asset.')
            except httpx.HTTPError:
                if attempt:
                    raise StitchError('The design was generated, but an artifact download failed. Use Check status to retry the download without generating again.', 'artifact_download_failed', 502) from None

async def offline_html(raw, assets):
    soup=BeautifulSoup(raw,'html.parser'); warnings=[]
    if not soup.html:
        soup=BeautifulSoup('<html><head></head><body>'+str(soup)+'</body></html>','html.parser')
    if not soup.head: soup.html.insert(0,soup.new_tag('head'))
    for element in soup(['base','iframe','object','embed','meta']): element.decompose()
    async def data_uri(url):
        if str(url).startswith('data:'):return str(url)
        data,mime=await assets.fetch(str(url))
        return 'data:'+mime+';base64,'+base64.b64encode(data).decode()
    async def css_local(css):
        if re.search(r'@import\s',css,re.I):
            css=re.sub(r'@import[^;]+;','',css,flags=re.I);warnings.append('An imported stylesheet was removed; its styles may be missing.')
        for match in list(re.finditer(r'url\(\s*[\'"]?([^\)\'\"]+)[\'"]?\s*\)',css)):
            url=match.group(1).strip()
            if url.startswith(('data:','#')):continue
            try: replacement=await data_uri(url)
            except Exception: replacement='';warnings.append('A font or background image could not be embedded.')
            css=css.replace(match.group(0),'url("'+replacement+'")')
        return css
    configs=[];tailwind=None
    for script in list(soup.find_all('script')):
        src=script.get('src','')
        if urlparse(src).hostname=='cdn.tailwindcss.com' and allowed(src):
            try:
                code,_=await assets.fetch(src);tailwind=code.decode('utf-8')
            except Exception:warnings.append('Tailwind styles could not be bundled.')
        elif not src and re.fullmatch(r'\s*tailwind\.config\s*=\s*(\{.*\})\s*;?\s*',script.get_text(),re.S):
            try:
                config=re.fullmatch(r'\s*tailwind\.config\s*=\s*(\{.*\})\s*;?\s*',script.get_text(),re.S).group(1)
                configs.append(json5.loads(config))
            except Exception:warnings.append('The design theme configuration could not be bundled.')
        elif src or script.get_text().strip():warnings.append('Custom scripts were removed; this preview is a visual prototype.')
        script.decompose()
    for tag in list(soup.find_all('link')):
        if 'stylesheet' in tag.get('rel',[]):
            try:
                data,_=await assets.fetch(tag.get('href',''));style=soup.new_tag('style');style.string=await css_local(data.decode('utf-8'));tag.replace_with(style)
            except Exception:tag.decompose();warnings.append('A stylesheet could not be embedded.')
        else:tag.decompose()
    for style in soup.find_all('style'):style.string=await css_local(str(style.string or ''))
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on') or attr in ('srcset','formaction','action','integrity','nonce'):del tag.attrs[attr]
        if tag.has_attr('style'):tag['style']=await css_local(tag['style'])
        if tag.name=='a':tag['href']='#'
        if tag.has_attr('src'):
            try:tag['src']=await data_uri(tag['src'])
            except Exception:del tag['src'];warnings.append('An image could not be embedded.')
    if tailwind:
        script=soup.new_tag('script');script.string=tailwind;soup.head.append(script)
        for config in configs:
            script=soup.new_tag('script');script.string='tailwind.config = '+json.dumps(config).replace('</','<\\/');soup.head.append(script)
    charset=soup.new_tag('meta');charset['charset']='utf-8';soup.head.insert(0,charset)
    policy=soup.new_tag('meta');policy['http-equiv']='Content-Security-Policy';policy['content']=CSP;soup.head.insert(1,policy)
    return '<!doctype html>\n'+str(soup),list(dict.fromkeys(warnings))

async def assemble(screens):
    assets=Assets();result=[]
    for row in screens:
        url=(row.get('htmlCode') or {}).get('downloadUrl')
        if not url:continue
        raw,_=await assets.fetch(url)
        local,warnings=await offline_html(raw.decode('utf-8'),assets)
        shot=(row.get('screenshot') or {}).get('downloadUrl');image=None
        if shot:
            try:
                data,mime=await assets.fetch(shot);image='data:'+mime+';base64,'+base64.b64encode(data).decode()
            except Exception:warnings.append('Screenshot download failed.')
        result.append({'name':row.get('name',''),'title':row.get('title') or 'UI screen',
                       'html':local,'screenshot':image,'warnings':warnings,'offline_complete':not any(w != 'Custom scripts were removed; this preview is a visual prototype.' for w in warnings)})
    if not result:raise StitchError('Stitch has not returned downloadable HTML yet. Use Check status.')
    return result

def bundle(screens):
    buf=io.BytesIO();manifest=[];links=[]
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:
        for n,row in enumerate(screens,1):
            name=f'screen-{n:02d}.html';archive.writestr(name,row['html'])
            links.append('<li><a href="'+name+'">'+html.escape(row['title'])+'</a></li>')
            if row.get('screenshot'):
                prefix,data=row['screenshot'].split(',',1);ext='png' if 'image/png' in prefix else 'jpg'
                archive.writestr(f'screen-{n:02d}.{ext}',base64.b64decode(data))
            manifest.append({'file':name,'title':row['title'],'offline_complete':row['offline_complete'],'warnings':row['warnings']})
        archive.writestr('index.html','<!doctype html><meta charset="utf-8"><title>Wooplix UI Designs</title><h1>UI Designs</h1><ul>'+''.join(links)+'</ul>')
        archive.writestr('manifest.json',json.dumps(manifest,indent=2))
        archive.writestr('README.txt','Open index.html in a browser after extracting this ZIP. Screenshots work offline. HTML is a visual prototype, not a working application. See manifest.json for asset warnings. Credentials are never included.\n')
    return buf.getvalue()
