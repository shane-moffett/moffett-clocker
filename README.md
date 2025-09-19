# Moffett Taskbar Clocker Helper

A tiny Windows tray app that reminds you to clock in/out based on activity.

---

## 1) Files & Folder

Place these in a folder, e.g. `C:\Projects\MoffettClocker\`:
- `C:\Projects\MoffettClocker\manual_clocker.py`
- `C:\Projects\MoffettClocker\moffett.ico`
- `C:\Projects\MoffettClocker\README.md`

Open **PowerShell** in that folder (Explorer → address bar → type `powershell` → Enter).

---

## 2) Install Python (and add to PATH)

- Install **Python 3.11+** from https://www.python.org/downloads/windows/
- During install, **tick “Add Python to PATH”**.
- Verify in PowerShell:

    (shows Python version)
    
        python --version

    (shows pip version)
    
        python -m pip --version

### If Python isn’t on PATH (manual add)

1. Windows **Start** → type **“Environment Variables”** → **Edit the system environment variables**.
2. Click **Environment Variables…**.
3. Under **User variables** (or **System variables**):
   - Select **Path** → **Edit** → **New** and add:
     - `C:\Users\<YourUser>\AppData\Local\Programs\Python\Python311\`
     - `C:\Users\<YourUser>\AppData\Local\Programs\Python\Python311\Scripts\`
     - (Adjust `Python311` if your version differs.)
4. Click **OK** on all dialogs, close and reopen PowerShell.
5. Re-run the version checks above.

*(If you prefer the Windows “Python Launcher” and it’s installed, `py -3` can be used instead of `python`. This README assumes `python` is on PATH.)*

---

## 3) Install Dependencies (once)

    python -m pip install --upgrade pip
    python -m pip install pystray Pillow

---

## 4) Run (Development)

**With console (shows logs/errors):**

    python C:\Projects\MoffettClocker\manual_clocker.py

**Without console (tray-only):**

    pythonw C:\Projects\MoffettClocker\manual_clocker.py

Notes:
- `pythonw` runs without a console window.
- If you don’t see the tray icon, it may be hidden behind the “^” overflow on the taskbar.
- Windows 11: **Settings → Personalization → Taskbar → Other system tray icons** → enable it.

---

## 5) Build a Single EXE (Optional)

    python -m pip install pyinstaller
    python -m PyInstaller --onefile --noconsole --icon C:\Projects\MoffettClocker\moffett.ico --name moffett_taskbar_clocker C:\Projects\MoffettClocker\manual_clocker.py

Output:

- `C:\Projects\MoffettClocker\dist\moffett_taskbar_clocker.exe`

Notes:
- The `.ico` is only required for the **build** step.
- The EXE runs without a console window.

---

## 8) Data Location & Quitting

- App data is stored at:

        %APPDATA%\MoffettClocker

- To quit: right-click the tray icon → **Quit**.

---

## 9) Repo Contents

- `manual_clocker.py`
- `moffett.ico`
- `README.md`
