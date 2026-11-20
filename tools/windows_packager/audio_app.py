# SPDX-License-Identifier: GPL-2.0-or-later
"""Simple WAV conversion interface; conversion algorithm is supplied by z5-extensions."""
import ctypes
import importlib.util
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path.cwd()


def convert_file(source, destination, converter):
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.suffix.lower() not in {'.wav', '.mp3'}:
        raise ValueError('Выберите файл WAV или MP3.')
    if source.resolve() == destination.resolve():
        raise ValueError('Выходной файл не может совпадать с исходным.')
    if destination.name.upper() not in {f'{n}.WAV' for n in range(1, 6)}:
        raise ValueError('文件名必须是 1.WAV 到 5.WAV。')
    if os.name == 'nt' and ctypes.windll.kernel32.GetDriveTypeW(destination.resolve().anchor) != 3:
        raise ValueError('请保存到电脑本地固定磁盘，再由您自行复制到存储卡。')
    decoded, _ = converter.read_input(source.resolve())
    converted, info = converter.convert(decoded)
    problem = converter.module_check(converted)
    if problem:
        raise ValueError(problem)
    with destination.open('xb') as out:
        out.write(converted)
    return info


def main():
    converter_path = ROOT / 'make_snd_wav.py'
    if not converter_path.exists():
        converter_path = ROOT.parent / 'make_snd_wav.py'
    spec = importlib.util.spec_from_file_location('wav_converter', converter_path)
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    root = tk.Tk()
    root.title('Z5 快门声音转换')
    root.geometry('480x250')
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    source, number = tk.StringVar(), tk.StringVar(value='1')
    ttk.Label(frame, text='Выберите WAV или MP3. Результат: 48 кГц, 16 бит, стерео, максимум 1,28 с.').pack(anchor='w', pady=5)
    row = ttk.Frame(frame)
    row.pack(fill='x', pady=5)
    ttk.Button(row, text='Выбрать WAV или MP3', command=lambda: source.set(filedialog.askopenfilename(filetypes=[('Audio', '*.wav *.mp3'), ('WAV', '*.wav'), ('MP3', '*.mp3')]) or source.get())).pack(side='left', padx=6)
    ttk.Entry(row, textvariable=source).pack(side='left', fill='x', expand=True)
    row2 = ttk.Frame(frame)
    row2.pack(fill='x', pady=5)
    ttk.Label(row2, text='声音编号').pack(side='left')
    ttk.Combobox(row2, textvariable=number, values=['1', '2', '3', '4', '5'], state='readonly', width=4).pack(side='left', padx=8)
    def run():
        output_dir = filedialog.askdirectory(title='选择电脑上的保存位置', initialdir=str(APP_DIR))
        if not output_dir:
            return
        destination = Path(output_dir) / (number.get() + '.WAV')
        try:
            info = convert_file(source.get(), destination, converter)
        except Exception as e:
            messagebox.showerror('转换失败', str(e))
            return
        warning = '\n音频过长，已截短并淡出。' if info['truncated'] else ''
        if info['clipped']:
            warning += '\n音频音量过高，可能削波。'
        messagebox.showinfo('转换完成', f'已保存到：\n{destination}\n请自行复制到卡上的 SHUTTER 文件夹。{warning}')
    ttk.Button(frame, text='转换并保存', command=run).pack(anchor='w', pady=10)
    ttk.Label(frame, text='MP3 требует ffmpeg. Файл не перезаписывается и на карту не записывается.').pack(anchor='w')
    root.mainloop()

if __name__ == '__main__':
    main()
