#!/usr/bin/env python3
"""
Simple setup script to ensure dependencies are installed
"""
import subprocess
import sys

def install_requirements():
    """Install requirements from requirements.txt"""
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'])
        print("✅ All dependencies installed successfully!")
    except subprocess.CalledProcessError as e:
        print(f"❌ Error installing dependencies: {e}")
        sys.exit(1)

if __name__ == "__main__":
    install_requirements()
