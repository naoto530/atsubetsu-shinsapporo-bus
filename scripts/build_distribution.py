"""Build the standalone preview and PWA ZIP from the current sources."""
from pathlib import Path
import base64,json,re,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1]
def build():
 out=ROOT/'outputs';out.mkdir(exist_ok=True)
 data=json.loads((ROOT/'data/timetable.json').read_text(encoding='utf-8'))
 html=(ROOT/'index.html').read_text(encoding='utf-8')
 html=re.sub(r'<link[^>]+rel="manifest"[^>]*>','',html)
 html=re.sub(r'<link[^>]+href="assets/[^>]+>','',html)
 html=re.sub(r'<link[^>]+href="styles.css"[^>]*>',lambda m:'<style>\n'+(ROOT/'styles.css').read_text(encoding='utf-8')+'\n</style>',html)
 for image in re.findall(r'src="(assets/[^\"]+)"',html):
  mime='image/png' if image.endswith('.png') else 'image/svg+xml'
  html=html.replace('src="'+image+'"','src="data:'+mime+';base64,'+base64.b64encode((ROOT/image).read_bytes()).decode()+'"')
 script=(ROOT/'app.js').read_text(encoding='utf-8')
 html=html.replace('<script src="app.js" defer></script>','<script type="application/json" id="embeddedTimetable">'+json.dumps(data,ensure_ascii=False).replace('</','<\\/')+'</script>\n<script>'+script.replace('</script','<\\/script')+'</script>')
 (out/'shinsapporo-bound-bus-preview.html').write_text(html,encoding='utf-8')
 files=['index.html','app.js','styles.css','sw.js','manifest.webmanifest','README.md','start-preview.bat','preview_server.py']
 paths=[ROOT/f for f in files]
 for folder in ['assets','data','scripts','.github']:
  paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
 dist=out/'shinsapporo-bound-bus-pwa';dist.mkdir(exist_ok=True)
 with zipfile.ZipFile(out/'shinsapporo-bound-bus-pwa.zip','w',zipfile.ZIP_DEFLATED) as z:
  for p in paths:
   rel=p.relative_to(ROOT);dest=dist/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest);z.write(p,str(rel).replace('\\','/'))
 print('Built standalone HTML and PWA ZIP')
if __name__=='__main__':build()
