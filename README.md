# TelePort: Interactive Telegram Conversation Report

TelePort (TELEgram rePORT) is an interactive tool for analyzing and visualizing Telegram chat histories using sentiment analysis and customizable data visualizations.

## Installation & Setup

To install the project requirements, run the following command in a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

In order to use the **dumper functionality** you need to set up a `.env` file:

```env
## example of '.env' file
SCRIPT_FOLDER = "."
DEVICE_ML = "cpu"

API_ID = "TELEGRAM-APP-ID"
API_HASH = "TELEGRAM-APP-HASH"
PHONE_NUMBER = "+39 XXXXXXXXXX"
USERNAME = "Mortafix"
```
To dump a Telegram conversation you need to create a Telegram app with your account at [this link](https://my.telegram.org/).  
After creating it, enter the `app ID` and `hash`, as well as the phone number and username.

The device specification is required for the model used for sentiment analysis
| Device | Specification |
| ------------- | ------------- |
| Processor | cpu |
| Graphic card | gpu |
| Nvidia graphic card | cuda |
| Mac M series | mps |

### Setup a password for a conversation

You can set a password to lock a conversation with a **password** by writing it in the `.env` file
```env
PASSWORD_USERNAME = "password"

# example with 'mortafix' chat
PASSWORD_MORTAFIX = "opsie"
```

## Dumping Conversations

To export Telegram conversations, run the following command, replacing `chat-name` with the name of the chat:

```bash
python3 utils/dumper.py <chat-name>
```

* You can also dump group chats with the `-g` parameter:
```bash
python3 utils/dumper.py -g <group-chat-id>
```
* You can rename a chat with `-n` parameter:
```bash
python3 utils/dumper.py <chat-name> -n "WowChat"
```
* You can update every dumped conversations with `-all` parameter:
```bash
python3 utils/dumper.py -all
```
* You can list last N conversations with `-l N` parameter:
```bash
python3 utils/dumper.py -l 10
```
> For all the other parameters use `-h`

### Sentiment Model

The model used for emotion and sentiment analysis is [Feel-IT](https://huggingface.co/MilaNLProc/feel-it-italian-emotion).

## Viewing the report

To start the application and view the interactive report, run the following command:

```bash
streamlit run app.py
```
View **an example** at [teleport.moris.dev](https://teleport.moris.dev).