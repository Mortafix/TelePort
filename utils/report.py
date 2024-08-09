from collections import Counter
from csv import reader
from datetime import datetime
from pickle import dump, load
from statistics import mean, stdev

from dotenv import load_dotenv
from emoji import EMOJI_DATA
from nltk import download
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize


def download_nltk_resources():
    nltk_resources = ["stopwords", "punkt"]
    for resource in nltk_resources:
        download(resource, quiet=True)


load_dotenv()
download_nltk_resources()


# ---- build


def messages_count(data):
    if isinstance(data, dict):
        return {user: messages_count(messages) for user, messages in data.items()}
    total = len(data)
    total_year, total_month = dict(), dict()
    for _, _, _, _, date, *_ in data:
        month_key = f"{date.month}.{date.year}"
        total_year[date.year] = total_year.get(date.year, 0) + 1
        total_month[month_key] = total_month.get(month_key, 0) + 1
    return total, total_year, total_month


def words_count(words):
    if isinstance(words, dict):
        return {user: words_count(w) for user, w in words.items()}
    return dict(Counter(words).most_common())


def phrase_count(words, n_words=2):
    if isinstance(words, dict):
        return {user: phrase_count(w, n_words) for user, w in words.items()}
    phrase_counts = Counter()
    prhases = [
        " ".join(words[i : i + n_words]) for i in range(len(words) - (n_words - 1))
    ]
    phrase_counts.update(prhases)
    return dict(phrase_counts.most_common())


def messages_length(data):
    if isinstance(data, dict):
        return {user: messages_length(messages) for user, messages in data.items()}
    msg_per_len = sorted(data, key=lambda x: -len(x[3]))[:3]
    msg_chars = [len(text) for _, _, _, text, *_ in data]
    return msg_per_len, msg_chars, mean(msg_chars), stdev(msg_chars)


def messages_types(data):
    if isinstance(data, dict):
        return {user: messages_types(messages) for user, messages in data.items()}
    types_counter = Counter([m_type for _, _, m_type, _, *_ in data])
    return dict(types_counter.most_common())


def messages_emoji(words):
    if isinstance(words, dict):
        return {user: messages_emoji(w) for user, w in words.items()}
    all_emoji = [char for char in words if char in EMOJI_DATA]
    return dict(Counter(all_emoji).most_common())


def messages_analysis(data):
    if isinstance(data, dict):
        return {user: messages_analysis(messages) for user, messages in data.items()}
    return Counter([msg[6] for msg in data]), Counter([msg[5] for msg in data])


# ---- Report class


class Report:
    def __init__(self, filepath=None):
        if filepath:
            self.filepath = filepath
            self.data_by_user = self.get_data_per_user(reader(open(filepath)))
            self.data = sum(self.data_by_user.values(), [])
            self.get_text_and_words()
            self.get_text_and_words_by_user()
            self.build()

    def get_data_per_user(self, csv_reader):
        data_by_user = dict()
        for m_id, user, m_type, text, date, emotion, sentiment, score in csv_reader:
            message = (
                int(m_id),
                user,
                m_type,
                text,
                datetime.fromisoformat(date),
                emotion,
                sentiment,
                float(score),
            )
            data_by_user[user] = data_by_user.get(user, []) + [message]
        return data_by_user

    def get_text_and_words(self):
        self.text = " ".join([message for _, _, _, message, *_ in self.data])
        self.all_words = word_tokenize(self.text.lower())
        stop_words = set(stopwords.words("italian"))
        self.text_words = [
            w for w in self.all_words if w.isalnum() and w not in stop_words
        ]

    def get_text_and_words_by_user(self):
        self.text_by_user = dict()
        self.all_words_by_user = dict()
        self.text_words_by_user = dict()
        for user, messages in self.data_by_user.items():
            self.text_by_user[user] = " ".join([m[3] for m in messages])
            self.all_words_by_user[user] = word_tokenize(
                self.text_by_user.get(user).lower()
            )
            stop_words = set(stopwords.words("italian"))
            self.text_words_by_user[user] = [
                w
                for w in self.all_words_by_user.get(user)
                if w.isalnum() and w not in stop_words
            ]

    def build(self):
        self.tot, self.tot_year, self.tot_month = messages_count(self.data)
        self.tot_per_user = messages_count(self.data_by_user)
        self.words = words_count(self.text_words)
        self.words_per_user = words_count(self.text_words_by_user)
        self.lengths, self.len_chars, self.len_mean, self.len_std = messages_length(
            self.data
        )
        self.lengths_per_user = messages_length(self.data_by_user)
        self.phrases_2 = phrase_count(self.text_words)
        self.phrases_3 = phrase_count(self.text_words, 3)
        self.phrases_2_per_user = phrase_count(self.text_words_by_user)
        self.phrases_3_per_user = phrase_count(self.text_words_by_user, 3)
        self.types = messages_types(self.data)
        self.types_per_user = messages_types(self.data_by_user)
        self.emojis = messages_emoji(self.all_words)
        self.emoji_per_user = messages_emoji(self.all_words_by_user)
        self.sentiments, self.emotions = messages_analysis(self.data)
        self.analysis_user = messages_analysis(self.data_by_user)

    def to_json(self, filepath):
        attributes_dict = {
            "filepath": self.filepath,
            "data_by_user": {
                user: [
                    {
                        "message_id": m_id,
                        "user": user,
                        "type": m_type,
                        "text": text,
                        "date": date.isoformat(),
                        "emotion": emot,
                        "sentiment": sent,
                        "score": score,
                    }
                    for m_id, user, m_type, text, date, emot, sent, score in messages
                ]
                for user, messages in self.data_by_user.items()
            },
            "data": [
                {
                    "message_id": m_id,
                    "user": user,
                    "type": m_type,
                    "text": text,
                    "date": date.isoformat(),
                    "emotion": emot,
                    "sentiment": sent,
                    "score": score,
                }
                for m_id, user, m_type, text, date, emot, sent, score in self.data
            ],
            "text": self.text,
            "all_words": self.all_words,
            "text_words": self.text_words,
            "text_by_user": self.text_by_user,
            "all_words_by_user": self.all_words_by_user,
            "text_words_by_user": self.text_words_by_user,
            "tot": self.tot,
            "tot_year": self.tot_year,
            "tot_month": self.tot_month,
            "tot_per_user": self.tot_per_user,
            "words": self.words,
            "words_per_user": self.words_per_user,
            "lengths": self.lengths,
            "len_chars": self.len_chars,
            "len_mean": self.len_mean,
            "len_std": self.len_std,
            "lengths_per_user": self.lengths_per_user,
            "phrases_2": self.phrases_2,
            "phrases_3": self.phrases_3,
            "phrases_2_per_user": self.phrases_2_per_user,
            "phrases_3_per_user": self.phrases_3_per_user,
            "types": self.types,
            "types_per_user": self.types_per_user,
            "emojis": self.emojis,
            "emoji_per_user": self.emoji_per_user,
            "sentiments": self.sentiments,
            "emotions": self.emotions,
            "analysis_user": self.analysis_user,
        }
        return dump(attributes_dict, open(filepath, "wb+"))


def build_report_from_json(filepath):
    data = load(open(filepath, "rb"))
    report = Report()
    report.filepath = filepath
    report.data_by_user = {
        user: [
            (
                message["message_id"],
                message["user"],
                message["type"],
                message["text"],
                datetime.fromisoformat(message["date"]),
                message["emotion"],
                message["sentiment"],
                float(message["score"]),
            )
            for message in messages
        ]
        for user, messages in data["data_by_user"].items()
    }
    report.data = sum(report.data_by_user.values(), [])
    report.text = data["text"]
    report.all_words = data["all_words"]
    report.text_words = data["text_words"]
    report.text_by_user = data["text_by_user"]
    report.all_words_by_user = data["all_words_by_user"]
    report.text_words_by_user = data["text_words_by_user"]
    report.tot = data["tot"]
    report.tot_year = data["tot_year"]
    report.tot_month = data["tot_month"]
    report.tot_per_user = data["tot_per_user"]
    report.words = data["words"]
    report.words_per_user = data["words_per_user"]
    report.lengths = [
        (*msg[:4], datetime.fromisoformat(msg[4]), msg[5:]) for msg in data["lengths"]
    ]
    report.len_chars = data["len_chars"]
    report.len_mean = data["len_mean"]
    report.len_std = data["len_std"]
    report.lengths_per_user = {
        user: [
            [(*msg[:4], datetime.fromisoformat(msg[4]), msg[5:]) for msg in messages],
            *data,
        ]
        for user, (messages, *data) in data["lengths_per_user"].items()
    }
    report.phrases_2 = data["phrases_2"]
    report.phrases_3 = data["phrases_3"]
    report.phrases_2_per_user = data["phrases_2_per_user"]
    report.phrases_3_per_user = data["phrases_3_per_user"]
    report.types = data["types"]
    report.types_per_user = data["types_per_user"]
    report.emojis = data["emojis"]
    report.emoji_per_user = data["emoji_per_user"]
    report.sentiments = data["sentiments"]
    report.emotions = data["emotions"]
    report.analysis_user = data["analysis_user"]
    return report
