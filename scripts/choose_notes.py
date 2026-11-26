"""Show the local OS picker so relative image files remain accessible to the server."""
import json
import tkinter as tk
from tkinter import filedialog

root = tk.Tk ()
root.withdraw ()
root.attributes ('-topmost', True)
try:
	paths = filedialog.askopenfilenames (parent=root, title='选择 Markdown 笔记（图片会自动忽略）', filetypes=[
		('Markdown 与图片', '*.md *.markdown *.png *.jpg *.jpeg *.gif *.webp *.svg'),
		('所有文件', '*.*')])
	print (json.dumps (list (paths), ensure_ascii=True))
finally:
	root.destroy ()
