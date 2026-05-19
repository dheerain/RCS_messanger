# RCS Messenger

RCS Messenger is a Python-based application designed to facilitate bulk messaging using the Rich Communication Services (RCS) protocol. This project includes modules for session management, API configuration, and automated message forwarding.

## Features
- Bulk message sending via RCS.
- Session management for maintaining active connections.
- API configuration for seamless integration.
- Automated message forwarding.
- Miscellaneous utility functions.

## Project Structure
```
RCS_Messanger/
├── pyproject.toml
├── pyrightconfig.json
├── rcs_bulk_sender_app.py
├── RCS_session.py
├── requirements.txt
├── 147/
├── chrome_session/
│   ├── CrashpadMetrics-active.pma
│   ├── DevToolsActivePort
│   ├── ...
├── Data/
│   ├── contact_infos.csv
├── modules/
│   ├── __init__.py
│   ├── api_config.py
│   ├── auto_forward.py
│   ├── misc.py
```

## Installation
1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd RCS_Messanger
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage
1. Configure the application by editing the `api_config.py` file in the `modules` directory.
2. Add contact information to `Data/contact_infos.csv`.
3. Run the bulk sender application:
   ```bash
   python rcs_bulk_sender_app.py
   ```

## Requirements
- Python 3.8 or higher
- Required Python packages (see `requirements.txt`)

## Contributing
Contributions are welcome! Please fork the repository and submit a pull request.

## License
This project is licensed under the MIT License. See the LICENSE file for details.

## Acknowledgments
- Thanks to all contributors and the open-source community for their support.