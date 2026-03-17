#!/usr/bin/env python3
"""
打包脚本 - 将参考文献工具打包成独立可执行文件

使用方法:
    python build_exe.py          # 打包 GUI 版本
    python build_exe.py --cli    # 同时打包命令行版本
    python build_exe.py --all    # 打包所有版本
"""

import os
import subprocess
import sys
import shutil
from pathlib import Path

# 项目根目录
ROOT_DIR = Path(__file__).parent.resolve()
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"

# 需要包含的数据文件
DATA_FILES = [
    ("domain_cookies.json", "."),
    ("reference_tool.config.example.json", "."),
    ("README_reference_tool.md", "."),
    ("cookies_setup_guide.md", "."),
]

# 需要包含的目录
DATA_DIRS = [
    "core",
    "site_handlers",
]

# 需要排除的大型模块（减小可执行文件大小）
EXCLUDE_MODULES = [
    "torch", "torchvision", "torchaudio",
    "tensorflow", "keras",
    "scipy", "numpy", "pandas",
    "matplotlib", "seaborn",
    "PIL", "cv2", "opencv",
    "sklearn", "scikit-learn",
    "jupyter", "ipython", "notebook",
    "pytest", "sphinx",
    "sympy",
    "sqlalchemy",
    "lxml",
    "win32com", "pythoncom", "pywintypes",
    "pygments",
    "openpyxl",
    "fsspec",
]


def clean_build():
    """清理构建目录"""
    print("Cleaning build directories...")
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Removed: {d}")


def build_gui():
    """打包 GUI 版本"""
    print("\n" + "=" * 50)
    print("Building GUI executable...")
    print("=" * 50)

    # 构建 hiddenimports
    hidden_imports = [
        "pypdf",
        "requests",
        "requests.adapters",
        "requests.cookies",
        "requests.sessions",
        "urllib3",
        "tqdm",
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinter.ttk",
        "xml.etree.ElementTree",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=ReferenceTool",
        "--windowed",  # 不显示控制台窗口
        "--onefile",   # 打包成单个文件
        "--clean",
        "--noconfirm",
    ]

    # 添加排除模块
    for mod in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", mod])

    # 添加 hidden imports
    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])

    # 添加数据文件
    for src, dst in DATA_FILES:
        src_path = ROOT_DIR / src
        if src_path.exists():
            cmd.extend(["--add-data", f"{src_path}{os.pathsep}{dst}"])

    # 添加数据目录
    for dir_name in DATA_DIRS:
        dir_path = ROOT_DIR / dir_name
        if dir_path.exists():
            cmd.extend(["--add-data", f"{dir_path}{os.pathsep}{dir_name}"])

    # 主脚本
    cmd.append(str(ROOT_DIR / "reference_tool_gui.py"))

    print(f"Running: {' '.join(cmd[:10])}...")
    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode == 0:
        exe_path = DIST_DIR / "ReferenceTool.exe"
        if exe_path.exists():
            print(f"\n✓ GUI executable created: {exe_path}")
            print(f"  Size: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
            return True
    return False


def build_cli():
    """打包命令行版本"""
    print("\n" + "=" * 50)
    print("Building CLI executable...")
    print("=" * 50)

    hidden_imports = [
        "pypdf",
        "requests",
        "requests.adapters",
        "requests.cookies",
        "requests.sessions",
        "urllib3",
        "tqdm",
        "xml.etree.ElementTree",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=reference_tool",
        "--console",   # 显示控制台
        "--onefile",
        "--clean",
        "--noconfirm",
    ]

    # 添加排除模块
    for mod in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", mod])

    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])

    for src, dst in DATA_FILES:
        src_path = ROOT_DIR / src
        if src_path.exists():
            cmd.extend(["--add-data", f"{src_path}{os.pathsep}{dst}"])

    for dir_name in DATA_DIRS:
        dir_path = ROOT_DIR / dir_name
        if dir_path.exists():
            cmd.extend(["--add-data", f"{dir_path}{os.pathsep}{dir_name}"])

    cmd.append(str(ROOT_DIR / "reference_tool.py"))

    print(f"Running: {' '.join(cmd[:10])}...")
    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode == 0:
        exe_path = DIST_DIR / "reference_tool.exe"
        if exe_path.exists():
            print(f"\n✓ CLI executable created: {exe_path}")
            print(f"  Size: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
            return True
    return False


def create_release_package():
    """创建发布包"""
    print("\n" + "=" * 50)
    print("Creating release package...")
    print("=" * 50)

    release_dir = DIST_DIR / "ReferenceTool_Release"
    release_dir.mkdir(exist_ok=True)

    # 复制可执行文件
    exe_files = [
        ("ReferenceTool.exe", "ReferenceTool.exe"),
        ("reference_tool.exe", "reference_tool.exe"),
    ]

    for src, dst in exe_files:
        src_path = DIST_DIR / src
        if src_path.exists():
            shutil.copy2(src_path, release_dir / dst)
            print(f"  Copied: {dst}")

    # 复制配置文件
    config_files = [
        "domain_cookies.json",
        "reference_tool.config.example.json",
        "README_reference_tool.md",
        "cookies_setup_guide.md",
    ]

    for f in config_files:
        src_path = ROOT_DIR / f
        if src_path.exists():
            shutil.copy2(src_path, release_dir / f)
            print(f"  Copied: {f}")

    # 创建 cookies 目录
    cookies_dir = release_dir / "cookies"
    cookies_dir.mkdir(exist_ok=True)
    print(f"  Created: cookies/")

    # 创建使用说明
    readme_path = release_dir / "使用说明.txt"
    readme_content = """参考文献下载工具 使用说明
================================

1. 双击 ReferenceTool.exe 启动图形界面
   - 或使用 reference_tool.exe 进行命令行操作

2. 首次使用：
   - 选择输入 PDF 文件
   - 设置输出目录
   - 点击"质量优先"应用推荐参数
   - 点击"运行"开始处理

3. 配置机构 Cookies（可选但推荐）：
   - 登录学校图书馆网站
   - 使用浏览器扩展导出 Cookies
   - 将 Cookies 文件放入 cookies/ 目录
   - 编辑 domain_cookies.json 配置

4. 输出说明：
   - downloads/: 下载的 PDF 文件
   - references.json: 完整结果数据
   - download_log.csv: 下载日志
   - suggested_cookies_config.json: Cookies 配置建议

详细文档请参考 README_reference_tool.md

问题反馈: https://github.com/your-repo/issues
"""
    readme_path.write_text(readme_content, encoding="utf-8")
    print(f"  Created: 使用说明.txt")

    print(f"\n✓ Release package created: {release_dir}")
    return release_dir


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build Reference Tool executables")
    parser.add_argument("--cli", action="store_true", help="Also build CLI version")
    parser.add_argument("--all", action="store_true", help="Build all versions")
    parser.add_argument("--no-clean", action="store_true", help="Don't clean build directories first")
    args = parser.parse_args()

    if not args.no_clean:
        clean_build()

    success = True

    # 构建 GUI 版本
    if not build_gui():
        success = False

    # 构建 CLI 版本
    if args.cli or args.all:
        if not build_cli():
            success = False

    # 创建发布包
    if success:
        create_release_package()

    print("\n" + "=" * 50)
    if success:
        print("✓ Build completed successfully!")
        print(f"  Output: {DIST_DIR / 'ReferenceTool_Release'}")
    else:
        print("✗ Build failed. Check the errors above.")
    print("=" * 50)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
