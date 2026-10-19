# -*- coding: utf-8 -*-
"""Conservative, font-generated one-letter header classifier. No game image assets."""
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

LETTERS = 'ADFXV∀'
FONTS = ('arial.ttf', 'segoeui.ttf', 'meiryo.ttc', 'YuGothR.ttc')

def _mask(crop):
    import cv2
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    a = np.asarray(rgb).astype(np.int16)
    # Bright, nearly achromatic title text on dark header background.
    hi = a.max(axis=2); lo = a.min(axis=2)
    return ((lo >= 125) & (hi-lo <= 48)).astype(np.uint8)

def _bbox(mask):
    yy, xx = np.nonzero(mask)
    if len(xx) < 12: return None
    return int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1)

def _norm(mask):
    b = _bbox(mask)
    if b is None:return None
    l,t,r,bt=b
    glyph = Image.fromarray((mask[t:bt,l:r]*255).astype('uint8'))
    # Keep aspect ratio, then center in a 48px square.
    ratio = min(42/glyph.width, 42/glyph.height)
    size=(max(1,round(glyph.width*ratio)),max(1,round(glyph.height*ratio)))
    glyph=glyph.resize(size, Image.Resampling.NEAREST)
    dst=Image.new('L',(48,48),0)
    dst.paste(glyph,((48-size[0])//2,(48-size[1])//2))
    return np.asarray(dst)>127

def measure(crop):
    m=_mask(crop); b=_bbox(m)
    if b is None:return None
    l,t,r,bt=b
    return {'width':r-l,'height':bt-t,'ratio':(r-l)/(bt-t),'mask':m}

def classify(crop):
    """Return (letter, diagnostics). Abstain on weak or ambiguous scores."""
    info=measure(crop)
    if info is None:return None, {'reason':'no ink'}
    w,h=info['width'],info['height']
    if not (13<=h<=32 and .55<=w/h<=1.38):
        return None, {'reason':'not single Latin-size glyph','width':w,'height':h}
    target=_norm(info['mask'])
    scores={}
    for font_name in FONTS:
        path=Path('C:/Windows/Fonts')/font_name
        if not path.is_file():continue
        for char in LETTERS:
            for size in (28,40,54):
                try:font=ImageFont.truetype(str(path),size)
                except OSError:continue
                canvas=Image.new('L',(96,96),0)
                draw=ImageDraw.Draw(canvas)
                draw.text((10,10),char,font=font,fill=255,stroke_width=0)
                norm=_norm((np.asarray(canvas)>125).astype('uint8'))
                if norm is None:continue
                union=np.count_nonzero(target|norm)
                score=np.count_nonzero(target&norm)/union if union else 0
                scores[char]=max(scores.get(char,0),score)
    if not scores:return None, {'reason':'no supported system fonts','width':w,'height':h}
    ordered=sorted(scores.items(),key=lambda x:x[1],reverse=True)
    best=ordered[0]; second=ordered[1] if len(ordered)>1 else ('',0)
    # Font variation is large: never guess if there is not strong evidence.
    accepted=best[1]>=.72 and best[1]-second[1]>=.16
    return (best[0] if accepted else None), {'width':w,'height':h,'best':best,'second':second,'accepted':accepted}
