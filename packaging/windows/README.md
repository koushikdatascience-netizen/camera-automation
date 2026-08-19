# Camera Automation Windows Packaging

This directory contains the build scripts and configuration for creating a production-ready Windows package.

## Build Process

### Prerequisites

1. Python 3.9+ installed
2. PyInstaller installed (`pip install pyinstaller`)
3. All project dependencies installed (`pip install -r requirements.txt`)

### Building the Application

Run the build script from PowerShell:

```powershell
.\packaging\windows\build_windows.ps1
```

This will create a production build in the `dist/CameraAutomation/` directory.

### Build Options

- **BuildType**: `"onedir"` (default) or `"onefile"`
- **OutputDir**: `"dist"` (default)
- **CleanBuild**: `$true` (default) to clean previous builds
- **IncludeDebugSymbols**: `$false` (default)

Example with custom options:

```powershell
.\packaging\windows\build_windows.ps1 -BuildType "onefile" -CleanBuild $true
```

### Build Output

The build process creates:

1. **Executable**: `dist/CameraAutomation/CameraAutomation.exe`
2. **Start Script**: `START_CAMERA_AUTOMATION.bat`
3. **Stop Script**: `STOP_CAMERA_AUTOMATION.bat`
4. **Configuration Example**: `.env.example`

### Running the Application

1. **Development Mode**:
   ```powershell
   python -m camera_service.launcher
   ```

2. **Production Mode**:
   ```powershell
   .\START_CAMERA_AUTOMATION.bat
   ```

3. **Stop the Application**:
   ```powershell
   .\STOP_CAMERA_AUTOMATION.bat
   ```

### Deployment Structure

The production deployment should have the following structure:

```
CameraAutomation/
├── CameraAutomation.exe          # Main executable
├── _internal/                    # PyInstaller runtime files
├── camera_service/               # Application code
├── data/                         # Application data
├── models/                       # AI models
├── logs/                         # Log files
├── config.yaml                   # Configuration
├── START_CAMERA_AUTOMATION.bat   # Start script
└── STOP_CAMERA_AUTOMATION.bat    # Stop script
```

### Data Directory

By default, the application uses `C:\ProgramData\CameraAutomation\` for persistent data storage including:

- Database files
- Camera snapshots
- Log files
- Evidence files

### Configuration

Copy `.env.example` to `.env` and modify as needed for your environment.

### Troubleshooting

1. **Missing dependencies**: Ensure all requirements are installed
2. **Port conflicts**: Make sure port 8000 is available
3. **Model files**: Ensure all AI model files are present in the `models/` directory
4. **Permissions**: Ensure the application has write access to the data directory

### Future Enhancements

- Inno Setup installer script for professional installation
- Windows Service wrapper for background operation
- Automatic model download and verification
- Advanced logging and monitoring