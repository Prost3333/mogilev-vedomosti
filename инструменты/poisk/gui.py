"""Графический интерфейс для poisk.py: те же команды, но кнопками.

Запуск:  py gui.py   (или двойной щелчок по poisk_gui.bat)
Нужен только tkinter (входит в Python). Всё выполняется через poisk.py в отдельном процессе.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # папка со сканами
POISK = HERE / "poisk.py"
WORK = HERE / "работа"
MODELS = ["", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("poisk — поиск упоминаний в сканах")
        self.geometry("980x720")
        self.proc = None
        self.q = queue.Queue()
        self.buttons = []
        self._build()
        self.after(100, self._pump)

    # ---------- интерфейс ----------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        top = ttk.LabelFrame(self, text="Что обрабатываем")
        top.pack(fill="x", padx=8, pady=6)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="PDF:").grid(row=0, column=0, sticky="nw", **pad)
        lf = ttk.Frame(top)
        lf.grid(row=0, column=1, sticky="ew", **pad)
        lf.columnconfigure(0, weight=1)
        self.pdf_list = tk.Listbox(lf, selectmode="extended", height=6, exportselection=False)
        lsb = ttk.Scrollbar(lf, command=self.pdf_list.yview)
        self.pdf_list.configure(yscrollcommand=lsb.set)
        self.pdf_list.grid(row=0, column=0, sticky="ew")
        lsb.grid(row=0, column=1, sticky="ns")
        for p in sorted(ROOT.glob("*.pdf")):
            self.pdf_list.insert("end", p.name)
        if self.pdf_list.size():
            self.pdf_list.selection_set(0)
        bf = ttk.Frame(top)
        bf.grid(row=0, column=2, sticky="n", **pad)
        ttk.Button(bf, text="Добавить файлы…", command=self._browse).pack(fill="x", pady=1)
        ttk.Button(bf, text="Добавить папку…", command=self._browse_dir).pack(fill="x", pady=1)
        ttk.Button(bf, text="Выбрать все", command=lambda: self.pdf_list.selection_set(0, "end")).pack(fill="x", pady=1)
        ttk.Button(bf, text="Убрать из списка", command=self._remove_sel).pack(fill="x", pady=1)

        ttk.Label(top, text="Страницы:").grid(row=1, column=0, sticky="w", **pad)
        self.pages = tk.StringVar()
        ttk.Entry(top, textvariable=self.pages, width=24).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(top, text="напр. 1-70 или 5,8,10-  (пусто = все)").grid(row=1, column=1, sticky="e", **pad)

        ttk.Label(top, text="Модель поиска:").grid(row=2, column=0, sticky="w", **pad)
        self.model = tk.StringVar()
        ttk.Combobox(top, textvariable=self.model, values=MODELS, width=24).grid(row=2, column=1, sticky="w", **pad)

        opt = ttk.Frame(top)
        opt.grid(row=3, column=0, columnspan=3, sticky="w")
        self.batch = tk.BooleanVar()
        self.all_ = tk.BooleanVar()
        ttk.Checkbutton(opt, text="Batch API (−50%, до 24 ч)", variable=self.batch).pack(side="left", **pad)
        ttk.Checkbutton(opt, text="Отчёт с отклонёнными", variable=self.all_).pack(side="left", **pad)

        ttk.Label(top, text="API-ключ:").grid(row=4, column=0, sticky="w", **pad)
        self.key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.key_entry = ttk.Entry(top, textvariable=self.key, show="•")
        self.key_entry.grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(top, text="Вставить", command=lambda: self._paste(self.key_entry)).grid(row=4, column=2, **pad)
        self._enable_clipboard(self.key_entry)

        steps = ttk.LabelFrame(self, text="Шаги")
        steps.pack(fill="x", padx=8, pady=4)
        spec = [
            ("0. Оценка стоимости", self.do_dry),
            ("1. Поиск (scan)", self.do_scan),
            ("1b. Забрать Batch", self.do_collect),
            ("2. Проверка API (платная)", self.do_verify),
            ("2а. Экспорт для Claude Code", self.do_export),
            ("2б. Импорт вердиктов", self.do_import),
            ("3. Отчёт", self.do_report),
            ("Статус", self.do_status),
        ]
        for i, (t, f) in enumerate(spec):
            b = ttk.Button(steps, text=t, command=f)
            b.grid(row=0, column=i, padx=4, pady=6, sticky="ew")
            steps.columnconfigure(i, weight=1)
            self.buttons.append(b)

        extra = ttk.LabelFrame(self, text="Дополнительно")
        extra.pack(fill="x", padx=8, pady=4)
        self.truth = tk.StringVar(value=str(ROOT / "данные" / "1892" / "упоминания.csv"))
        ttk.Label(extra, text="Эталон CSV:").grid(row=0, column=0, **pad)
        ttk.Entry(extra, textvariable=self.truth).grid(row=0, column=1, sticky="ew", **pad)
        extra.columnconfigure(1, weight=1)
        b = ttk.Button(extra, text="Eval", command=self.do_eval); b.grid(row=0, column=2, **pad); self.buttons.append(b)
        b = ttk.Button(extra, text="Расшифровать стр.", command=self.do_transcribe); b.grid(row=0, column=3, **pad); self.buttons.append(b)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=2)
        self.stop_btn = ttk.Button(bar, text="■ Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Открыть папку «работа»", command=lambda: self._open(WORK)).pack(side="left", padx=4)
        ttk.Button(bar, text="Открыть находки.xlsx", command=lambda: self._open(WORK / "находки.xlsx")).pack(side="left", padx=4)
        ttk.Button(bar, text="Очистить лог", command=lambda: self.log.delete("1.0", "end")).pack(side="right", padx=4)
        self.state_lbl = ttk.Label(bar, text="готов")
        self.state_lbl.pack(side="right", padx=8)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(frame, wrap="word", font=("Consolas", 10))
        sb = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ---------- вспомогательное ----------
    def _paste(self, w):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        try:
            w.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        w.insert("insert", text)

    def _enable_clipboard(self, w):
        """Ctrl+V/C/X/A при любой раскладке (по коду клавиши) и меню правой кнопки."""
        def on_key(e):
            act = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}.get(e.keycode)
            if act == "<<Paste>>":
                self._paste(w)
            elif act:
                w.event_generate(act)
            elif e.keycode == 65:
                w.select_range(0, "end")
            else:
                return None
            return "break"
        w.bind("<Control-KeyPress>", on_key)
        menu = tk.Menu(w, tearoff=0)
        menu.add_command(label="Вставить", command=lambda: self._paste(w))
        menu.add_command(label="Вырезать", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="Очистить", command=lambda: w.delete(0, "end"))
        w.bind("<Button-3>", lambda e: (w.focus_set(), menu.tk_popup(e.x_root, e.y_root)))

    def _add_pdfs(self, paths):
        have = set(self.pdf_list.get(0, "end"))
        for p in paths:
            p = str(Path(p))
            if p not in have:
                self.pdf_list.insert("end", p)
                have.add(p)
            i = self.pdf_list.get(0, "end").index(p)
            self.pdf_list.selection_set(i)

    def _browse(self):
        ps = filedialog.askopenfilenames(initialdir=ROOT, filetypes=[("PDF", "*.pdf")])
        self._add_pdfs(ps)

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=ROOT)
        if d:
            self._add_pdfs(sorted(Path(d).glob("*.pdf")))

    def _remove_sel(self):
        for i in reversed(self.pdf_list.curselection()):
            self.pdf_list.delete(i)

    def _open(self, path):
        path = Path(path)
        if not path.exists():
            messagebox.showinfo("Нет файла", f"Ещё не создано:\n{path}")
            return
        os.startfile(path)

    def _pdf_paths(self):
        sel = [self.pdf_list.get(i) for i in self.pdf_list.curselection()]
        if not sel:
            messagebox.showwarning("PDF", "Выберите один или несколько PDF в списке")
            return None
        out = []
        for p in sel:
            path = Path(p)
            if not path.is_absolute():
                path = ROOT / path
            if not path.exists():
                messagebox.showwarning("PDF", f"Файл не найден:\n{path}")
                return None
            out.append(str(path))
        return out

    def _pdf_path(self):
        """Один PDF (для eval/transcribe)."""
        ps = self._pdf_paths()
        if not ps:
            return None
        if len(ps) > 1:
            messagebox.showwarning("PDF", "Для этой команды выберите один PDF")
            return None
        return ps[0]

    def _opts(self, pages=True, model=True):
        a = []
        if pages and self.pages.get().strip():
            a += ["--pages", self.pages.get().strip()]
        if model and self.model.get().strip():
            a += ["--model", self.model.get().strip()]
        return a

    # ---------- команды ----------
    def do_dry(self):
        p = self._pdf_paths()
        if p:
            self.run(["scan", *p, "--dry-run", *self._opts()] + (["--batch"] if self.batch.get() else []))

    def do_scan(self):
        p = self._pdf_paths()
        if p:
            self.run(["scan", *p, *self._opts()] + (["--batch"] if self.batch.get() else []))

    def do_collect(self):
        self.run(["collect", "--wait"])

    def do_verify(self):
        a = ["verify"]
        if self.model.get().strip():
            a += ["--scan-model", self.model.get().strip()]
        self.run(a)

    def do_export(self):
        a = ["review-export"]
        if self.model.get().strip():
            a += ["--scan-model", self.model.get().strip()]
        self.run(a)
        self.log.insert("end", "Дальше: скажите Claude Code «проверь кандидатов из работа/проверка», затем нажмите «Импорт вердиктов».\n")

    def do_import(self):
        self.run(["review-import"])

    def do_report(self):
        a = ["report"]
        if self.all_.get():
            a.append("--all")
        if self.model.get().strip():
            a += ["--model", self.model.get().strip()]
        self.run(a)

    def do_status(self):
        self.run(["status"])

    def do_eval(self):
        p = self._pdf_path()
        if p:
            self.run(["eval", p, "--truth", self.truth.get().strip(), *self._opts()])

    def do_transcribe(self):
        p = self._pdf_path()
        if not p:
            return
        if not self.pages.get().strip():
            messagebox.showwarning("Страницы", "Для расшифровки укажите страницы (дорого — весь текст).")
            return
        self.run(["transcribe", p, *self._opts()])

    # ---------- запуск процесса ----------
    def run(self, args):
        if self.proc:
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if self.key.get().strip():
            env["ANTHROPIC_API_KEY"] = self.key.get().strip()
        py = Path(sys.executable).with_name("python.exe")
        cmd = [str(py) if py.exists() else sys.executable, "-u", str(POISK), *args]
        self.log.insert("end", "\n> poisk.py " + " ".join(args) + "\n")
        self.log.see("end")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.log.insert("end", f"Не удалось запустить: {e}\n")
            return
        self._busy(True)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.q.put(line)
        proc.wait()
        self.q.put(("done", proc.returncode))

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self.proc = None
                    self._busy(False)
                    rc = item[1]
                    self.log.insert("end", "✔ готово\n" if rc == 0 else f"✖ завершено с кодом {rc}\n")
                else:
                    self.log.insert("end", item)
                self.log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _busy(self, on):
        for b in self.buttons:
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        self.state_lbl.configure(text="работает…" if on else "готов")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.log.insert("end", "Остановка… (прогресс сохранён, повторный запуск продолжит)\n")


if __name__ == "__main__":
    App().mainloop()
