import json, os, pickle, re, shutil, subprocess, tempfile, time
from datetime import datetime, timezone
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT='Vimeo Backup'; MAPS='Maps'; HLS='HLS'; VIDEO_MAP='video-map.json'; HLS_MAP='hls-map.json'
MAX=max(1,int(os.getenv('HLS_MAX_ITEMS_PER_RUN','1') or '1'))
ONLY=str(os.getenv('HLS_ONLY_VIDEO_ID','') or '').strip()
FORCE=str(os.getenv('HLS_FORCE_REBUILD','0'))=='1'
SEG=6; ABR=96; DRIVE_RETRIES=10; DRIVE_CHUNK=16*1024*1024
ENCODER_PRESET=str(os.getenv('HLS_ENCODER_PRESET','superfast') or 'superfast').strip()
PARALLEL_SLOT_RAW=str(os.getenv('HLS_PARALLEL_SLOT','') or '').strip()
PARALLEL_SLOT=int(PARALLEL_SLOT_RAW) if PARALLEL_SLOT_RAW else None
PARALLEL_RESULT_DIR=str(os.getenv('HLS_PARALLEL_RESULT_DIR','') or '').strip()
if PARALLEL_SLOT is not None and PARALLEL_SLOT < 0: raise ValueError('HLS_PARALLEL_SLOT must be >= 0')
LADDER=[(360,550,700,1100),(480,900,1200,1800),(720,1600,2200,3200),(1080,2800,4000,5600)]
started=time.monotonic(); cache={}

def log(*args):
    print(*args, flush=True)

def esc(s): return str(s).replace("'","\\'")
def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
def safe_id(v):
    v=str(v or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',v): raise ValueError(f'Unsafe ID: {v!r}')
    return v

def execute(req):
    return req.execute(num_retries=DRIVE_RETRIES)

def folder(name,parent=None):
    k=(parent or '',name)
    if k in cache:return cache[k]
    q=f"name='{esc(name)}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:q+=f" and '{parent}' in parents"
    r=execute(drive.files().list(q=q,spaces='drive',fields='files(id)',pageSize=100)).get('files',[])
    if r: fid=r[0]['id']
    else:
        body={'name':name,'mimeType':'application/vnd.google-apps.folder'}
        if parent:body['parents']=[parent]
        fid=execute(drive.files().create(body=body,fields='id'))['id']; log('Created folder:',name)
    cache[k]=fid; return fid

def find(name,parent):
    q=f"name='{esc(name)}' and '{parent}' in parents and trashed=false"
    r=execute(drive.files().list(q=q,spaces='drive',fields='files(id,name)',pageSize=100)).get('files',[])
    return r[0] if r else None

def read_json(name,parent,default):
    f=find(name,parent)
    if not f:return default
    raw=execute(drive.files().get_media(fileId=f['id']))
    return json.loads(raw.decode('utf-8'))

def upload(path,parent,mime):
    filename=os.path.basename(path)
    old=find(filename,parent)
    media=MediaFileUpload(path,mimetype=mime,resumable=True,chunksize=DRIVE_CHUNK)
    if old:
        req=drive.files().update(fileId=old['id'],media_body=media,fields='id')
        log('Updating Drive:',filename)
    else:
        req=drive.files().create(body={'name':filename,'parents':[parent]},media_body=media,fields='id')
        log('Uploading Drive:',filename)
    out=None; last=-1
    while out is None:
        st,out=req.next_chunk(num_retries=DRIVE_RETRIES)
        if st:
            pct=int(st.progress()*100)
            if pct!=last:
                log(f'  {filename}: {pct}%')
                last=pct
    log(f'  {filename}: 100%')
    return out['id']

def download(fid,path):
    log('Downloading source MP4 from Drive...')
    with open(path,'wb') as h:
        d=MediaIoBaseDownload(h,drive.files().get_media(fileId=fid),chunksize=32*1024*1024)
        done=False; last=-1
        while not done:
            st,done=d.next_chunk(num_retries=DRIVE_RETRIES)
            if st:
                pct=int(st.progress()*100)
                if pct!=last:
                    log(f'  source.mp4: {pct}%')
                    last=pct
    if not os.path.getsize(path):raise RuntimeError('Drive MP4 download was empty')
    log('  source.mp4: 100%')

def probe(path):
    p=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',path],capture_output=True,text=True,check=True)
    s=json.loads(p.stdout)['streams'][0]; return int(s['width']),int(s['height'])

def even(n):
    n=max(2,int(round(n))); return n if n%2==0 else n-1

def renditions(w,h):
    r=[{'height':hh,'width':even(w*hh/h),'bitrate':b,'maxrate':m,'buf':buf} for hh,b,m,buf in LADDER if hh<=h]
    return r or [{'height':even(h),'width':even(w),'bitrate':450,'maxrate':600,'buf':900}]

def make_hls(src,out,rs):
    os.makedirs(out,exist_ok=True); n=len(rs); labels=''.join(f'[v{i}]' for i in range(n))
    fc=[f'[0:v:0]split={n}{labels}']+[f'[v{i}]scale=-2:{r["height"]}[vo{i}]' for i,r in enumerate(rs)]
    cmd=['ffmpeg','-hide_banner','-loglevel','warning','-y','-i',src,'-filter_complex',';'.join(fc)]
    for i,r in enumerate(rs):
        h=r['height']; pl=os.path.join(out,f'{h}p.m3u8'); media=os.path.join(out,f'{h}p.ts')
        cmd += ['-map',f'[vo{i}]','-map','0:a:0?','-c:v','libx264','-preset',ENCODER_PRESET,'-profile:v','main','-pix_fmt','yuv420p','-b:v',f'{r["bitrate"]}k','-maxrate',f'{r["maxrate"]}k','-bufsize',f'{r["buf"]}k','-sc_threshold','0','-force_key_frames',f'expr:gte(t,n_forced*{SEG})','-c:a','aac','-b:a',f'{ABR}k','-ac','2','-ar','48000','-hls_time',str(SEG),'-hls_playlist_type','vod','-hls_flags','single_file+independent_segments','-hls_segment_type','mpegts','-hls_segment_filename',media,pl]
    cmd += ['-progress','pipe:1','-stats_period','30','-nostats']
    log('Generating:',', '.join(f"{r['height']}p" for r in rs),f'(x264 preset: {ENCODER_PRESET})')
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    last_time=''
    assert proc.stdout is not None
    for line in proc.stdout:
        line=line.strip()
        if line.startswith('out_time='):
            value=line.split('=',1)[1]
            if value and value!=last_time:
                log('  encoded through',value)
                last_time=value
        elif line and ('Error' in line or 'error' in line.lower()):
            log('  ffmpeg:',line)
    rc=proc.wait()
    if rc!=0:raise RuntimeError(f'FFmpeg failed with exit code {rc}')
    for r in rs:
        p=os.path.join(out,f"{r['height']}p.m3u8")
        if '#EXT-X-BYTERANGE' not in open(p,encoding='utf-8').read():raise RuntimeError('HLS byte-range playlist missing')
    master=os.path.join(out,'master.m3u8'); lines=['#EXTM3U','#EXT-X-VERSION:6','#EXT-X-INDEPENDENT-SEGMENTS']
    for r in rs:
        avg=(r['bitrate']+ABR)*1000; peak=int((r['maxrate']+ABR)*1000*1.10)
        lines += [f"#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={avg},RESOLUTION={r['width']}x{r['height']}",f"{r['height']}p.m3u8"]
    open(master,'w',encoding='utf-8',newline='\n').write('\n'.join(lines)+'\n')
    log('HLS encoding complete')
    return master

def save_map(items,maps):
    os.makedirs(ROOT,exist_ok=True); p=os.path.join(ROOT,HLS_MAP)
    with open(p,'w',encoding='utf-8') as h: json.dump(items,h,indent=2,ensure_ascii=False)
    upload(p,maps,'application/json')

creds=None
if os.path.exists('token.pickle'):
    with open('token.pickle','rb') as h:creds=pickle.load(h)
if not creds:raise RuntimeError('token.pickle missing')
if creds.expired and creds.refresh_token:creds.refresh(Request())
with open('token.pickle','wb') as h:pickle.dump(creds,h)
drive=build('drive','v3',credentials=creds)
root=folder(ROOT); maps=folder(MAPS,root); hls_root=folder(HLS,root)
video_map=read_json(VIDEO_MAP,maps,None)
if not isinstance(video_map,list):raise RuntimeError('Existing video-map.json missing/invalid')
hls_map=read_json(HLS_MAP,maps,[])
if not isinstance(hls_map,list):hls_map=[]
idx={str(x.get('vimeoId') or ''):x for x in hls_map}
pending=[]
for x in reversed(video_map):
    vid=str(x.get('vimeoId') or '').strip(); drive_id=str(x.get('videoDriveId') or '').strip()
    if not vid or not drive_id or (ONLY and vid!=ONLY):continue
    if not FORCE and idx.get(vid,{}).get('masterDriveId'):continue
    pending.append(x)
if PARALLEL_SLOT is None:
    selected=pending[:MAX]
else:
    selected=pending[PARALLEL_SLOT:PARALLEL_SLOT+1]
log('MP4 entries:',len(video_map),'HLS complete:',len(idx),'pending:',len(pending),'this run:',len(selected),'parallel slot:',PARALLEL_SLOT if PARALLEL_SLOT is not None else 'off')
fail=[]; done=0
for x in selected:
    vid=safe_id(x['vimeoId']); title=str(x.get('title') or vid); tmp=tempfile.mkdtemp(prefix=f'irgun-hls-{vid}-')
    try:
        src=os.path.join(tmp,'source.mp4'); out=os.path.join(tmp,'hls'); log('\nHLS:',vid,title)
        download(str(x['videoDriveId']),src)
        w,h=probe(src); log(f'Source resolution: {w}x{h}')
        rs=renditions(w,h); master=make_hls(src,out,rs)
        dest=folder(vid,hls_root); uploaded=[]
        for r in rs:
            hh=r['height']; pl=f'{hh}p.m3u8'; media=f'{hh}p.ts'
            uploaded.append({'height':hh,'width':r['width'],'bitrateKbps':r['bitrate'],'maxrateKbps':r['maxrate'],'playlist':pl,'playlistDriveId':upload(os.path.join(out,pl),dest,'application/vnd.apple.mpegurl'),'media':media,'mediaDriveId':upload(os.path.join(out,media),dest,'video/mp2t')})
        entry={'vimeoId':vid,'title':title,'videoDriveId':str(x['videoDriveId']),'hlsVersion':1,'storage':'google-drive','format':'hls-single-file-byterange-mpegts','hlsFolderId':dest,'master':'master.m3u8','masterDriveId':upload(master,dest,'application/vnd.apple.mpegurl'),'sourceWidth':w,'sourceHeight':h,'renditions':uploaded,'generatedAt':now()}
        if PARALLEL_SLOT is not None:
            if not PARALLEL_RESULT_DIR: raise RuntimeError('HLS_PARALLEL_RESULT_DIR is required in parallel mode')
            os.makedirs(PARALLEL_RESULT_DIR,exist_ok=True)
            result_path=os.path.join(PARALLEL_RESULT_DIR,f'{vid}.json')
            with open(result_path,'w',encoding='utf-8') as h: json.dump(entry,h,indent=2,ensure_ascii=False)
            log('Staged parallel result:',result_path)
        else:
            if vid in idx:idx[vid].clear(); idx[vid].update(entry)
            else:hls_map.append(entry); idx[vid]=entry
            save_map(hls_map,maps)
        done+=1; log('Completed:',vid)
    except Exception as e:
        fail.append((vid,str(e))); log('ERROR:',vid,repr(e))
    finally:
        shutil.rmtree(tmp,ignore_errors=True)
if PARALLEL_SLOT is None:
    os.makedirs(ROOT,exist_ok=True)
    with open(os.path.join(ROOT,HLS_MAP),'w',encoding='utf-8') as h: json.dump(hls_map,h,indent=2,ensure_ascii=False)
log(f'Completed {done}; failed {len(fail)}; runtime {(time.monotonic()-started)/60:.1f} min')
if fail:raise RuntimeError('; '.join(f'{v}: {e}' for v,e in fail))
