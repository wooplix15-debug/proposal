import asyncio
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
import zipfile
from fastapi.testclient import TestClient
from api import index as api
import stitch_client as client
import stitch_export as export

SAMPLE='<html><head><script src="https://cdn.tailwindcss.com"></script><script>tailwind.config={theme:{extend:{colors:{brand:"#123456"}}}};</script><script>fetch("https://evil.test/")</script></head><body><h1 onclick="steal()">Bookings</h1><img src="https://lh3.googleusercontent.com/example"><a href="https://evil.test/">Continue</a></body></html>'

class ExportTests(IsolatedAsyncioTestCase):
 async def test_export_is_local_and_removes_untrusted_code(self):
  assets=export.Assets()
  async def fetch(url):
   if 'tailwind' in url:return b'window.tailwind={};','application/javascript'
   return b'image-data','image/png'
  assets.fetch=fetch
  html,warnings=await export.offline_html(SAMPLE,assets)
  self.assertNotIn('fetch("https://evil',html)
  self.assertNotIn('onclick=',html)
  self.assertNotIn('src="https://',html)
  self.assertIn('data:image/png;base64',html)
  self.assertIn('Content-Security-Policy',html)
  self.assertIn('tailwind.config',html)
  self.assertTrue(warnings)
 async def test_failed_asset_is_reported(self):
  assets=export.Assets();assets.fetch=AsyncMock(side_effect=RuntimeError('offline'))
  html,warnings=await export.offline_html('<html><head></head><body><img src="https://unknown.test/a"></body></html>',assets)
  self.assertTrue(warnings);self.assertNotIn('src="https://',html)
 async def test_downloaded_css_and_font_rules_are_preserved(self):
  assets=export.Assets();assets.fetch=AsyncMock(return_value=(b'.icons {font-family: iconfont}', 'text/css'))
  html,warnings=await export.offline_html('<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/test"></head><body></body></html>',assets)
  self.assertIn('.icons {font-family: iconfont}',html)
  self.assertEqual(warnings,[])
 def test_local_and_unapproved_hosts_are_rejected(self):
  for url in ['http://127.0.0.1/x','https://localhost/x','https://googleusercontent.com.evil.test/x','https://user:pass@lh3.googleusercontent.com/a']:
   self.assertFalse(export.allowed(url))
 def test_zip_has_only_local_artifacts_and_manifest(self):
  row={'html':'<h1>UI</h1>','title':'Booking UI','offline_complete':True,'warnings':[],'screenshot':None}
  with zipfile.ZipFile(io.BytesIO(export.bundle([row]))) as z:
   self.assertEqual(set(z.namelist()),{'index.html','screen-01.html','manifest.json','README.txt'})
   self.assertTrue(json.loads(z.read('manifest.json'))[0]['offline_complete'])

class ApiTests(TestCase):
 def test_stitch_browser_script_is_served_at_vercel_api_route(self):
  response=TestClient(api.app).get('/api/stitch_ui.js')
  self.assertEqual(response.status_code,200)
  self.assertIn('Calling the original handler',response.text)

 def setUp(self):
  api._UI_CACHE.clear();self.web=TestClient(api.app)
 def test_access_is_required(self):
  self.assertEqual(self.web.get('/api/ui-design/config').status_code,401)
 def test_missing_auth_has_honest_feedback(self):
  with patch.object(api,'_require_allowed_user'),patch.dict(os.environ,{},clear=True):
   r=self.web.post('/api/ui-design/prepare',data={'text':'Customers manage service bookings.'})
  self.assertEqual(r.status_code,503);self.assertIn('STITCH_API_KEY',r.json()['detail'])
 def test_prepare_validates_before_creating_project(self):
  with patch.object(api,'_require_allowed_user'),patch.object(client,'configured',return_value=True),patch.object(client,'create_project',new_callable=AsyncMock) as create:
   r=self.web.post('/api/ui-design/prepare',data={'text':'short'})
   self.assertEqual(r.status_code,400);create.assert_not_called()
 def test_prepare_and_generate_use_the_signed_brief(self):
  context={'ui_design':True,'project_id':'123','brief':'Approved booking BRD','screen':'Dashboard','device':'DESKTOP'}
  result={'outputComponents':[{'design':{'screens':[{'name':'projects/123/screens/a'}]}}]}
  row={'html':'<h1>UI</h1>','title':'UI','offline_complete':True,'warnings':[],'screenshot':None}
  with patch.object(api,'_require_allowed_user'),patch.object(api.workflow,'unseal',return_value=context),patch.object(api.workflow,'seal',return_value='new-token'),patch.object(client,'generate',new_callable=AsyncMock,return_value=result) as generate,patch.object(export,'assemble',new_callable=AsyncMock,return_value=[row]):
   response=self.web.post('/api/ui-design/generate',data={'project_token':'signed'})
  self.assertEqual(response.status_code,200);self.assertEqual(response.json()['project_token'],'new-token');generate.assert_awaited_once_with('123','Approved booking BRD','Dashboard','DESKTOP')
 def test_generated_screen_reference_survives_export_failure(self):
  context={'ui_design':True,'project_id':'123','brief':'Approved booking BRD','screen':'Dashboard','device':'DESKTOP'}
  result={'outputComponents':[{'design':{'screens':[{'name':'projects/123/screens/a'}]}}]}
  with patch.object(api,'_require_allowed_user'),patch.object(api.workflow,'unseal',return_value=context),patch.object(api.workflow,'seal',return_value='recoverable-token'),patch.object(client,'generate',new_callable=AsyncMock,return_value=result),patch.object(export,'assemble',new_callable=AsyncMock,side_effect=client.StitchError('Download failed')):
   response=self.web.post('/api/ui-design/generate',data={'project_token':'signed'})
   self.assertEqual(response.json()['detail']['project_token'],'recoverable-token')
 def test_status_and_download_never_generate_again(self):
  context={'ui_design':True,'project_id':'123','screen_names':['projects/123/screens/a']}
  row={'html':'<h1>UI</h1>','title':'UI','offline_complete':True,'warnings':[],'screenshot':None}
  with patch.object(api,'_require_allowed_user'),patch.object(api.workflow,'unseal',return_value=context),patch.object(client,'screens',new_callable=AsyncMock,return_value=[{}]) as read_screens,patch.object(export,'assemble',new_callable=AsyncMock,return_value=[row]),patch.object(client,'generate',new_callable=AsyncMock) as generate:
   self.assertEqual(self.web.post('/api/ui-design/status',data={'project_token':'signed'}).status_code,200)
   response=self.web.post('/api/ui-design/download',data={'project_token':'signed'})
   self.assertEqual(response.status_code,200);self.assertEqual(response.headers['content-type'],'application/zip');generate.assert_not_called();read_screens.assert_awaited_once()
 def test_other_agent_token_is_rejected(self):
  with patch.object(api,'_require_allowed_user'),patch.object(api.workflow,'unseal',return_value={'document_kind':'ux'}):
   self.assertEqual(self.web.post('/api/ui-design/generate',data={'project_token':'signed'}).status_code,400)
 def test_key_is_not_in_config(self):
  with patch.object(api,'_require_allowed_user'),patch.dict(os.environ,{'STITCH_API_KEY':'secret-test-value'}):
   result=self.web.get('/api/ui-design/config');self.assertNotIn('secret-test-value',result.text)

class ClientTests(TestCase):
 def test_auth_failure_is_not_exposed(self):
  import httpx
  request=httpx.Request('POST','https://stitch.googleapis.com/mcp',headers={'X-Goog-Api-Key':'secret'})
  error=httpx.HTTPStatusError('sensitive message',request=request,response=httpx.Response(401,request=request))
  mapped=client.classify(error);self.assertEqual(mapped.code,'authentication_failed');self.assertNotIn('sensitive',str(mapped))
 def test_generation_response_extracts_only_screens(self):
  self.assertEqual(client.generated_screens({'outputComponents':[{'text':'Clarify'},{'design':{'screens':[{'name':'screen'}]}}]}),[{'name':'screen'}])
