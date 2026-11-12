from argparse import ArgumentParser
from csv import reader, writer
from os import getenv, mkdir, path, walk
from re import search
from time import sleep
from warnings import simplefilter

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.tl.types import (MessageMediaContact, MessageMediaDocument,
                               MessageMediaGeo, MessageMediaPhoto,
                               MessageMediaUnsupported)
from tqdm import tqdm
from transformers import pipeline

simplefilter(action="ignore", category=FutureWarning)
load_dotenv()
BATCH_SIZE = 500


def get_message_type(message):
    if message.text:
        return "text"
    if message.sticker:
        return "sticker"
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        if message.file.mime_type.startswith("video"):
            return "video"
        if message.file.mime_type.startswith("audio"):
            return "audio"
        return "document"
    if isinstance(message.media, MessageMediaContact):
        return "contact"
    if isinstance(message.media, MessageMediaGeo):
        return "location"
    if isinstance(message.media, MessageMediaUnsupported):
        return "unsupported"
    return "unknown"


def clean_message(text):
    if not text:
        return None
    text = text.replace("\n", " ")
    exclude_chars = "«»"
    for char in exclude_chars:
        text = text.replace(char, "")
    return text


def get_recent_chats(n_chats):
    client = TelegramClient("chatty", getenv("API_ID"), getenv("API_HASH"))
    client.start(phone=getenv("PHONE_NUMBER"))
    for dialog in client.iter_dialogs(n_chats):
        print(f"Chat: {dialog.name} • ID: {dialog.entity.id}")
    client.disconnect()


def conversation_dump(username, renaming=None, is_group=False, use_first_name=True):
    # init telegram app
    client = TelegramClient("chatty", getenv("API_ID"), getenv("API_HASH"))
    client.start(phone=getenv("PHONE_NUMBER"))

    # init classifier
    emotion_classifier = pipeline(
        "text-classification",
        model="MilaNLProc/feel-it-italian-emotion",
        device=getenv("DEVICE_ML"),
    )

    # create output folder
    conversations_folder = path.join(getenv("SCRIPT_FOLDER"), "conversations")
    if not path.exists(conversations_folder):
        mkdir(conversations_folder)
    filename = f"{renaming.replace(' ', '_') or username}"
    filename += f"_[{'g-' if is_group else ''}{username}].csv"
    dump_file = path.join(conversations_folder, filename)

    # check if report already exists
    last_id, msgs_dumped = 0, 0
    if path.exists(dump_file):
        with open(dump_file) as csv_file:
            data = list(reader(csv_file))
            if data:
                last_id = int(data[-1][0])
                msgs_dumped = len(data)

    if is_group:
        username = client.get_entity(int(username))

    with client:
        total_messages = client.get_messages(username, limit=0).total - msgs_dumped
        if not total_messages:
            return

        chat_users = dict()
        # get messages from chat in batch
        for i in tqdm(range(0, total_messages, BATCH_SIZE)):
            messages = client.get_messages(
                username, limit=BATCH_SIZE, min_id=last_id, reverse=True
            )
            last_id = messages[-1].id
            data = list()
            for message in messages:
                # check if real message
                user = message.sender_id
                if not user:
                    continue
                # sentiment analysis
                message_text = clean_message(message.text)
                message_type = get_message_type(message)
                analysis = ("-", "-", 0)
                if message_text:
                    try:
                        guess = emotion_classifier(message_text)[0]
                        analysis = (
                            guess.get("label"),
                            "positive" if guess.get("label") == "joy" else "negative",
                            guess.get("score"),
                        )
                    except Exception:
                        pass

                # add to data
                if user not in chat_users:
                    user_entity = client.get_entity(user)
                    chat_users[user] = (
                        user_entity.first_name
                        if use_first_name
                        else user_entity.username
                    )
                data.append(
                    [
                        message.id,
                        chat_users.get(user),
                        message_type,
                        message_text,
                        message.date,
                        *analysis,
                    ]
                )

            # save to csv
            with open(dump_file, "a+") as csvfile:
                writer(csvfile).writerows(data)

            # stay below API rate limit
            sleep(0.5)


def args_parser():
    parser = ArgumentParser(
        prog="dumper",
        description="Dump every Telegram conversation",
        usage="dumper [CHAT_ID_OR_USERNAME] [-l 10] [-g]",
        epilog="Example: dumper Mortafix",
    )
    parser.add_argument(
        "chat",
        nargs="?",
        default=None,
        help="Telegram username or chat ID",
    )
    parser.add_argument(
        "-l", "--list", type=int, help="list last N Telegram chats", metavar=("N")
    )
    parser.add_argument("--name", type=str, help="rename chat", metavar=("NAME"))
    parser.add_argument(
        "-g", "--group", action="store_true", help="chat ID provided is a group chat"
    )
    parser.add_argument(
        "--all", action="store_true", help="updated every chat already dumped"
    )
    parser.add_argument(
        "--use-username",
        action="store_true",
        help="use usernames instead of first names",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = args_parser()

    # list
    if args.list:
        get_recent_chats(args.list)
        exit()

    # update every chat
    if args.all:
        conversation_folder = path.join(getenv("SCRIPT_FOLDER"), "conversations")
        csv_files = list(walk(conversation_folder))[0][2]
        for file in csv_files:
            filename, extension = path.splitext(file)
            if extension != ".csv" or filename == "example_[example]":
                continue
            renaming, is_group, username = search(
                r"(\w+)_\[(g\-)?(\w+)\]", filename
            ).groups()
            print(f"> Updating {renaming.replace('_', ' ')} conversation")
            conversation_dump(username, renaming, bool(is_group))
        exit()

    # dump chat
    conversation_dump(args.chat, args.name, args.group, not args.use_username)
