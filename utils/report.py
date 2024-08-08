from collections import Counter
from csv import reader
from datetime import datetime
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
    def __init__(self, filepath):
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
