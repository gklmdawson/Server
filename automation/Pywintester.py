from pywinauto import Desktop
for w in Desktop(backend="uia").windows():
    print(repr(w.window_text()), "|", w.class_name())