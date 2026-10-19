# -*- coding: utf-8 -*-
"""Optional Windows.Media.Ocr fallback for single-character pre-play center titles.
Adapted from the user's successful first-hit standalone experiment.
"""
import asyncio
import io
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance

CANDIDATES = (
    ('P1_binary50_black', .5, 0, 'binary', 'black'),
    ('P2_binary50_white', .5, 0, 'binary', 'white'),
    ('P3_original100_black', 1., 0, 'original', 'black'),
    ('P4_original100_white', 1., 0, 'original', 'white'),
    ('P5_binary75_black', .75, 0, 'binary', 'black'),
    ('P6_contrast75_black', .75, 0, 'contrast', 'black'),
)

def _font():
    for name in ('YuGothB.ttc', 'meiryob.ttc', 'meiryo.ttc', 'msgothic.ttc'):
        path = Path('C:/Windows/Fonts') / name
        if path.is_file():
            return ImageFont.truetype(str(path), 72)
    raise FileNotFoundError('Japanese font not found')

def _trim(image):
    box = image.convert('L').point(lambda v: 255 if v >= 100 else 0).getbbox()
    if not box:
        return None
    l, t, r, b = box
    return image.crop((max(0,l-2),max(0,t-2),min(image.width,r+2),min(image.height,b+2)))

def _tile(font, height):
    bounds = ImageDraw.Draw(Image.new('L',(1,1))).textbbox((0,0),'あ',font=font)
    w,h=bounds[2]-bounds[0],bounds[3]-bounds[1]
    tile=Image.new('L',(max(1,w),max(1,h)),0)
    ImageDraw.Draw(tile).text((-bounds[0],-bounds[1]),'あ',font=font,fill=255)
    return tile.resize((max(1,round(tile.width*height/tile.height)),height),Image.Resampling.LANCZOS)

def _compose(glyph,font,scale,gap,mode,bg):
    gray=glyph.convert('L')
    if mode=='binary': ink=gray.point(lambda v: 255 if v>=125 else 0)
    elif mode=='contrast': ink=ImageEnhance.Contrast(gray).enhance(2.5)
    else: ink=gray
    a=_tile(font,ink.height)
    pieces=(a,ink,a)
    margin=12
    canvas=Image.new('L',(sum(x.width for x in pieces)+2*gap+2*margin,ink.height+2*margin),0)
    x=margin
    for part in pieces:
        canvas.paste(part,(x,margin));x+=part.width+gap
    if bg=='white': canvas=ImageOps.invert(canvas)
    size=(max(1,round(canvas.width*scale)),max(1,round(canvas.height*scale)))
    if size!=canvas.size: canvas=canvas.resize(size,Image.Resampling.LANCZOS)
    return canvas.convert('RGB')

def _extract(text):
    s=''.join(text.split())
    if len(s)!=3 or s[0]!='あ' or not (s[2]=='あ' or 'ぁ'<=s[2]<='ん'):
        return ''
    c=s[1]; n=ord(c)
    return c if (0x3400<=n<=0x9FFF or 0xF900<=n<=0xFAFF or 0x20000<=n<=0x3134F or c in '〆々') else ''

async def _ocr(image,engine):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    data=io.BytesIO();image.save(data,format='PNG')
    stream=InMemoryRandomAccessStream();writer=DataWriter(stream)
    try:
        writer.write_bytes(data.getvalue())
        await writer.store_async();await writer.flush_async();writer.detach_stream()
    finally:writer.close()
    stream.seek(0)
    try:
        decoder=await BitmapDecoder.create_async(stream)
        bitmap=await decoder.get_software_bitmap_async()
        result=await engine.recognize_async(bitmap)
        return result.text
    finally:stream.close()

async def _run(glyph):
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    engine=OcrEngine.try_create_from_language(Language('ja-JP'))
    if engine is None:raise RuntimeError('Japanese Windows OCR language unavailable')
    font=_font()
    for label,scale,gap,mode,bg in CANDIDATES:
        text=await _ocr(_compose(glyph,font,scale,gap,mode,bg),engine)
        candidate=_extract(text)
        print(f'[CENTER OCR {label}] raw={text!r} extracted={candidate!r}',flush=True)
        if candidate:return candidate,label
    return None,None

def recognize_center_single(frame_bgr,roi):
    import cv2
    x1,y1,x2,y2=roi
    h,w=frame_bgr.shape[:2]
    if w!=1920 or h!=1080:
        x1,x2=round(x1*w/1920),round(x2*w/1920)
        y1,y2=round(y1*h/1080),round(y2*h/1080)
    crop=frame_bgr[y1:y2,x1:x2]
    if crop.size==0:return None,None,0.
    glyph=_trim(Image.fromarray(cv2.cvtColor(crop,cv2.COLOR_BGR2RGB)))
    if glyph is None:return None,None,0.
    started=time.perf_counter()
    candidate,label=asyncio.run(_run(glyph))
    return candidate,label,(time.perf_counter()-started)*1000


def recognize_header_title(crop_bgr, lang="ja-JP"):
    """Recognize the standard small header title using Windows.Media.Ocr.

    Return [(variant, text, elapsed_ms), ...]. Do not interpret a blank OCR
    result as a song title. All work happens in the existing OCR worker thread.
    """
    import cv2
    from PIL import Image
    if crop_bgr is None or crop_bgr.size == 0:
        return []
    rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    original = Image.fromarray(rgb)

    async def run():
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        engine = OcrEngine.try_create_from_language(Language(lang))
        if engine is None:
            raise RuntimeError(f'Windows OCR language unavailable: {lang}')
        variants = (
            ('up3', original.resize((original.width*3, original.height*3), Image.Resampling.LANCZOS)),
            ('up2', original.resize((original.width*2, original.height*2), Image.Resampling.LANCZOS)),
            ('original', original),
        )
        outputs = []
        for label, img in variants:
            t0 = time.perf_counter()
            text = (await _ocr(img, engine)).strip().replace('\n', ' ')
            outputs.append((label, text, (time.perf_counter()-t0)*1000))
        return outputs
    return asyncio.run(run())
