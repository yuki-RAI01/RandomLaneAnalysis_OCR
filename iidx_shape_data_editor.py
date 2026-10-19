# -*- coding: utf-8 -*-
"""IIDX one-letter header shape CSV editor. Python 3.10+, Pillow, numpy, opencv-python.
Run: python -u iidx_shape_data_editor.py
"""
import csv
import io
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk
import numpy as np
import cv2
import iidx_shape_features as sf

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / 'data' / 'iidx_single_letter_shapes.csv'
CONFIG_PATH = ROOT / 'config' / 'iidx_shape_editor.json'

# iidx_main.py TITLE_ROI_BY_LAYOUT (1920x1080): centers of title regions.
# Keep the compact single-letter ROI centered within the main title ROI.
MAIN_TITLE_ROIS = {
    '1P_左': (1155, 55, 1725, 95),
    '1P_右': (738, 55, 1308, 95),
    '2P_左': (612, 55, 1182, 95),
    '2P_右': (196, 55, 766, 95),
}

def default_roi(profile):
    x1, _, x2, _ = MAIN_TITLE_ROIS[profile]
    base = sf.DEFAULT_ROI
    delta = round((x1 + x2) / 2 - (base[0] + base[2]) / 2)
    return [base[0] + delta, base[1], base[2] + delta, base[3]]


class Editor:
    def __init__(self, root):
        self.root = root
        root.title('IIDX 1文字形状データ作成ツール')
        root.geometry('1220x890')
        self.cfg = self.load_cfg()
        self.side = tk.StringVar(value='1P')
        self.graph = tk.StringVar(value='左')
        self.letter = tk.StringVar(value='A')
        self.sample_id = tk.StringVar(value='')
        self.status = tk.StringVar(value='1920×1080の画像を開いてください')
        self.frame = None
        self.photo = None
        self.roi = list(sf.DEFAULT_ROI)
        self.features = None
        self.diagnostics = None
        self.drag = None
        self.scale = 1
        self.offset = (0, 0)

        top = ttk.Frame(root, padding=(12, 9))
        top.pack(fill='x')
        ttk.Label(top, text='プレーサイド:').pack(side='left')
        for val in ('1P','2P'):
            ttk.Radiobutton(top, text=val, variable=self.side, value=val, command=self.switch_profile).pack(side='left', padx=6)
        ttk.Label(top, text='スコアグラフ:').pack(side='left', padx=(18,3))
        for val in ('左','右'):
            ttk.Radiobutton(top, text=val, variable=self.graph, value=val, command=self.switch_profile).pack(side='left', padx=6)

        row = ttk.Frame(root, padding=(12, 2))
        row.pack(fill='x')
        ttk.Button(row, text='スクリーンショットを開く (1920×1080)', command=self.open_image).pack(side='left')
        ttk.Label(row, text='対象文字:').pack(side='left', padx=(18,4))
        ttk.Entry(row, textvariable=self.letter, width=6).pack(side='left')
        ttk.Label(row, text='サンプルID:').pack(side='left', padx=(18,4))
        ttk.Entry(row, textvariable=self.sample_id, width=24).pack(side='left')
        ttk.Button(row, text='数値化', command=self.analyze).pack(side='left', padx=10)

        self.canvas = tk.Canvas(root, bg='#1d222a', highlightthickness=0, height=560)
        self.canvas.pack(fill='both', expand=True, padx=12, pady=(8,2))
        self.canvas.bind('<Configure>', lambda _e:self.redraw())
        self.canvas.bind('<ButtonPress-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_move)
        self.canvas.bind('<ButtonRelease-1>', lambda _e:self.mouse_up())

        adjust = ttk.Frame(root, padding=(12,4))
        adjust.pack(fill='x')
        ttk.Label(adjust, text='上部曲名の範囲 (x1, y1, x2, y2):').pack(side='left')
        self.coords = [tk.StringVar() for _ in range(4)]
        for var in self.coords:
            ttk.Entry(adjust, textvariable=var, width=7).pack(side='left', padx=3)
        ttk.Button(adjust, text='座標を反映', command=self.apply_coords).pack(side='left', padx=8)
        ttk.Button(adjust, text='初期位置', command=self.reset_roi).pack(side='left')
        ttk.Label(adjust, text='赤枠内ドラッグ=移動／赤枠外ドラッグ=新しい範囲', foreground='#555').pack(side='left', padx=14)

        bottom = ttk.Frame(root, padding=(12,6))
        bottom.pack(fill='x')
        ttk.Button(bottom, text='CSV行をコピー', command=self.copy_row).pack(side='left', padx=(0,8))
        ttk.Button(bottom, text='CSVに追加・更新', command=self.save_row).pack(side='left')
        ttk.Button(bottom, text='CSVを開く', command=self.show_csv).pack(side='left', padx=8)
        ttk.Label(bottom, text='保存先: data/iidx_single_letter_shapes.csv').pack(side='left', padx=14)
        ttk.Label(root, textvariable=self.status, padding=(12,0,12,5), wraplength=1150).pack(anchor='w')
        self.result = tk.Text(root, height=5, wrap='none', font=('Consolas', 10))
        self.result.pack(fill='x', padx=12, pady=(0,10))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.switch_profile()

    def load_cfg(self):
        try:
            return json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}

    def key(self):
        return self.side.get() + '_' + self.graph.get()

    def persist(self):
        self.cfg[self.key()] = self.roi[:]
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding='utf-8')

    def switch_profile(self):
        # Align each profile with the corresponding main-program title ROI.
        # Previously saved manual positions still take precedence.
        vals = self.cfg.get(self.key(), default_roi(self.key()))
        self.roi = list(map(int, vals))
        self.sync_coords()
        self.features = None
        self.redraw()

    def sync_coords(self):
        for v, x in zip(self.coords, self.roi):v.set(str(x))

    def reset_roi(self):
        self.roi = default_roi(self.key())
        self.sync_coords()
        self.persist()
        self.redraw()

    def apply_coords(self):
        try:
            coords = [int(x.get()) for x in self.coords]
            x1,y1,x2,y2 = coords
            if not (0<=x1<x2<=1920 and 0<=y1<y2<=1080):raise ValueError('座標範囲が不正')
            self.roi = coords
            self.persist()
            self.features = None
            self.redraw()
        except ValueError as exc:
            messagebox.showerror('座標エラー', str(exc))

    def open_image(self):
        path = filedialog.askopenfilename(title='1920×1080のスクリーンショット', filetypes=[('画像','*.png *.jpg *.jpeg *.bmp *.webp')])
        if not path:return
        try:
            with Image.open(path) as im:
                if im.size != sf.FRAME_SIZE:
                    raise ValueError(f'1920×1080が必要です（画像は{im.width}×{im.height}）')
                rgb = np.asarray(im.convert('RGB'))
            self.frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            self.sample_id.set(Path(path).stem)
            self.features = None
            self.status.set(f'読み込み: {path}  |  赤枠を調整して「数値化」を押してください')
            self.redraw()
        except (OSError, ValueError) as exc:
            messagebox.showerror('画像エラー', str(exc))

    def redraw(self):
        if not hasattr(self,'canvas'):return
        c=self.canvas;c.delete('all')
        cw,ch=c.winfo_width(),c.winfo_height()
        if cw<20 or ch<20:return
        scale=min((cw-10)/1920,(ch-10)/1080)
        ox=(cw-1920*scale)/2;oy=(ch-1080*scale)/2
        self.scale,self.offset=scale,(ox,oy)
        if self.frame is not None:
            rgb=cv2.cvtColor(self.frame,cv2.COLOR_BGR2RGB)
            image=Image.fromarray(rgb).resize((round(1920*scale),round(1080*scale)))
            self.photo=ImageTk.PhotoImage(image)
            c.create_image(ox,oy,anchor='nw',image=self.photo)
        else:
            c.create_rectangle(ox,oy,ox+1920*scale,oy+1080*scale,outline='#8793a0',fill='#101820')
            c.create_text(cw/2,ch/2,text='画像を読み込んでください',fill='white')
        x1,y1,x2,y2=self.roi
        c.create_rectangle(ox+x1*scale,oy+y1*scale,ox+x2*scale,oy+y2*scale,outline='#ff4343',width=3)
        c.create_text(ox+x1*scale,oy+y1*scale-4,anchor='sw',text=f'{self.side.get()} / グラフ{self.graph.get()}  TITLE',fill='#ff5555')

    def point(self, event):
        return ((event.x-self.offset[0])/self.scale,(event.y-self.offset[1])/self.scale)

    def mouse_down(self,event):
        x,y=self.point(event)
        x1,y1,x2,y2=self.roi
        mode='move' if x1<=x<=x2 and y1<=y<=y2 else 'new'
        self.drag=(mode,x,y,self.roi[:])

    def mouse_move(self,event):
        if not self.drag:return
        mode,sx,sy,original=self.drag
        x,y=self.point(event)
        if mode=='move':
            dx=int(round(x-sx));dy=int(round(y-sy))
            x1,y1,x2,y2=original
            dx=max(-x1,min(1920-x2,dx));dy=max(-y1,min(1080-y2,dy))
            self.roi=[x1+dx,y1+dy,x2+dx,y2+dy]
        else:
            self.roi=[max(0,int(min(sx,x))),max(0,int(min(sy,y))),min(1920,int(max(sx,x))),min(1080,int(max(sy,y)))]
        self.sync_coords();self.redraw()

    def mouse_up(self):
        if self.drag:
            self.drag=None
            if self.roi[2]>self.roi[0] and self.roi[3]>self.roi[1]:
                self.persist()
                self.features=None

    def analyze(self):
        if self.frame is None:
            messagebox.showwarning('画像なし','画像を読み込んでください');return
        if len(self.letter.get().strip())!=1:
            messagebox.showwarning('対象文字','対象文字は1文字にしてください');return
        try:
            self.features,self.diagnostics=sf.extract(self.frame,self.roi)
            self.persist()
            try:
                rows=sf.read_csv(CSV_PATH)
                candidate,details=sf.compare(self.features,rows)
            except ValueError as e:
                candidate,details=None,{'CSV error':str(e)}
            self.result.delete('1.0','end')
            self.result.insert('end',f'対象={self.letter.get().strip()}  ink={self.diagnostics["width"]}x{self.diagnostics["height"]}  bbox={self.diagnostics["bbox"]}\n')
            self.result.insert('end',f'既存CSVとの比較: candidate={candidate!r}  {details}\n')
            self.result.insert('end',f'数値列={len(sf.FEATURE_NAMES)}  CSV行コピー／CSV追加・更新ができます。')
            self.status.set('数値化しました。対象文字と抽出範囲を確認してCSVに保存してください。')
        except ValueError as e:
            self.features=None
            messagebox.showerror('数値化できません',str(e))

    def current_row(self):
        if self.features is None:
            raise ValueError('先に「数値化」を実行してください')
        letter=self.letter.get().strip()
        if len(letter)!=1:raise ValueError('対象文字は1文字にしてください')
        sample=self.sample_id.get().strip() or letter+'_sample'
        return {'letter':letter,'version':sf.VERSION,'sample_id':sample,**self.features}

    def copy_row(self):
        try:
            row=self.current_row()
            output=io.StringIO()
            writer=csv.DictWriter(output,fieldnames=sf.COLUMNS)
            writer.writerow({k:f'{row[k]:.6f}' if k in sf.FEATURE_NAMES else row[k] for k in sf.COLUMNS})
            self.root.clipboard_clear();self.root.clipboard_append(output.getvalue().strip())
            self.status.set('CSVデータ行をクリップボードにコピーしました')
        except ValueError as e:messagebox.showerror('コピーできません',str(e))

    def save_row(self):
        try:
            row=self.current_row()
            rows=sf.read_csv(CSV_PATH)
            # Stable sample ID permits replacing a sample without duplicate rows.
            for i,old in enumerate(rows):
                if old['letter']==row['letter'] and old['sample_id']==row['sample_id']:
                    if not messagebox.askyesno('更新確認','同じ文字・サンプルIDの行を上書きしますか？'):return
                    rows[i]=row
                    break
            else:rows.append(row)
            sf.write_csv(CSV_PATH,rows)
            self.status.set(f'保存しました: {CSV_PATH}（{len(rows)}行）')
            messagebox.showinfo('保存完了',f'{row["letter"]} / {row["sample_id"]}\n{CSV_PATH}')
        except (OSError,ValueError) as e:messagebox.showerror('CSV保存エラー',str(e))

    def show_csv(self):
        if not CSV_PATH.exists():messagebox.showinfo('CSVなし',f'まだCSVがありません: {CSV_PATH}');return
        import os
        if hasattr(os,'startfile'):os.startfile(str(CSV_PATH))
        else:messagebox.showinfo('CSV保存先',str(CSV_PATH))

    def close(self):
        self.root.destroy()


def main():
    root=tk.Tk();Editor(root);root.mainloop()

if __name__=='__main__':main()
