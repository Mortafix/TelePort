from csv import reader, writer
from os import getenv, mkdir, path, walk
from sys import argv
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


def conversation_dump(chat_name):
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
    dump_file = path.join(conversations_folder, f"{chat_name}.csv")

    # check if report already exists
    last_id, msgs_dumped = 0, 0
    if path.exists(dump_file):
        with open(dump_file) as csv_file:
            data = list(reader(csv_file))
            if data:
                last_id = int(data[-1][0])
                msgs_dumped = len(data)

    # get personal info
    me = client.get_me()
    user_nick = me.username
    user_id = me.id

    with client:
        total_messages = client.get_messages(chat_name, limit=0).total - msgs_dumped
        if not total_messages:
            return

        # get messages from chat in batch
        for i in tqdm(range(0, total_messages, BATCH_SIZE)):
            messages = client.get_messages(
                chat_name, limit=BATCH_SIZE, min_id=last_id, reverse=True
            )
            last_id = messages[-1].id
            data = list()
            for message in messages:
                message_text = clean_message(message.text)
                message_type = get_message_type(message)
                # sentiment analysis
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
                data.append(
                    [
                        message.id,
                        user_nick if message.sender_id == user_id else chat_name,
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


if __name__ == "__main__":
    chat_name = argv[-1]
    if chat_name == "--all":
        conversation_folder = path.join(getenv("SCRIPT_FOLDER"), "conversations")
        csv_files = list(walk(conversation_folder))[0][2]
        for file in csv_files:
            chat_name, extension = path.splitext(file)
            if extension != ".csv":
                continue
            print(f"> Updating {chat_name} conversation")
            conversation_dump(chat_name)
    else:
        conversation_dump(chat_name)
