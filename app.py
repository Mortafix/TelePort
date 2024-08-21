from collections import Counter
from datetime import datetime
from os import getenv, path, walk

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from utils.cache import (get_json_report, remove_all_json_report,
                         save_json_report)
from utils.charts import BarChart, DoubleBarChart, LineChart, PieChart
from utils.report import Report

st.set_page_config(
    page_title="TelePort",
    page_icon="📡",
    layout="centered",
    menu_items=None,
)

load_dotenv()
MEDALS = ["🥇", "🥈", "🥉"]


def get_dataset():
    conversation_folder = path.join(getenv("SCRIPT_FOLDER"), "conversations")
    csv_file = list(walk(conversation_folder))[0][2]
    return {
        path.splitext(file)[0]: path.abspath(path.join(conversation_folder, file))
        for file in csv_file
        if path.splitext(file)[1] == ".csv"
    }


@st.cache_data(persist=True, show_spinner="Building **report**..")
def get_report(filepath):
    if report := get_json_report(filepath):
        return report
    report = Report(filepath)
    save_json_report(report)
    return report


@st.dialog("Conversation is locked 🔒")
def password_wall(chat):
    st.write(f"Input **password** for :gray-background[{chat}] conversation report")
    password = st.text_input(
        "Password", type="password", placeholder=f"Password for {chat}"
    )
    if st.button("Unlock", use_container_width=True):
        if password == getenv(f"PASSWORD_{chat.upper()}"):
            st.session_state[f"logged_{chat}"] = True
            st.rerun()
        st.error("**Wrong** password..", icon="❌")


def app():
    st.title("TelePort")
    st.subheader("Interactive Telegram Conversation Report ")
    st.write(
        ":gray-background[TelePort] (_TELEgram rePORT_) is an interactive report "
        "designed to analyze and visualize your Telegram conversations. By leveraging "
        "advanced sentiment analysis and customizable data visualizations, TelePort "
        "provides deep insights into your chat history. Discover trends, track "
        "sentiment changes over time, and explore your conversations in a whole new way"
        " with this user-friendly tool."
    )
    st.divider()

    # choose dataset
    if st.button("Clear cache 🗑️"):
        st.cache_data.clear()
        st.session_state.clear()
        remove_all_json_report()
        st.rerun()
    DATASETS = get_dataset()
    if username_selection := st.selectbox(
        "Conversations", DATASETS, None, placeholder="Choose a conversation"
    ):
        st.session_state.username = username_selection
    if not st.session_state.get("username"):
        return
    username = st.session_state.username

    # insert password
    chat_password = getenv(f"PASSWORD_{username.upper()}")
    if chat_password and not st.session_state.get(f"logged_{username}"):
        password_wall(username)
        if st.session_state.get(f"logged_{username}"):
            st.rerun()
        return

    report = get_report(DATASETS.get(username))
    st.divider()

    # report
    st.header(f"Conversation report with :blue[@{username}]", divider="blue")
    st.markdown(
        f"Messages range from **{report.data[0][4]:%d.%m.%Y}** to "
        f"**{report.data[-1][4]:%d.%m.%Y}** for a total of **{report.tot}** messages"
    )

    # ---- frequency
    st.subheader("Message frequency", divider="gray")
    cols = st.columns(len(report.tot_per_user) + 1)
    cols[0].metric("Total messages", report.tot)
    for i, (user, user_tot) in enumerate(report.tot_per_user.items(), 1):
        cols[i % 3].metric(
            f"Total :gray-background[**{user}**] messages",
            user_tot[0],
            delta_color="off",
        )
    # pie chart | messages per users
    freq = {user: data[0] for user, data in report.tot_per_user.items()}
    pie_chart = PieChart(freq)
    pie_chart.build()
    _, center_col, _ = st.columns((1, 3, 1))
    center_col.pyplot(pie_chart.fig, use_container_width=True)

    # bar chart | messages per year
    most_year = sorted(report.tot_year, key=lambda x: -report.tot_year.get(x))[0]
    most_month = sorted(report.tot_month, key=lambda x: -report.tot_month.get(x))[0]
    most_msg_month_label = datetime(*map(int, most_month.split(".")[::-1]), 1)
    st.markdown(
        f"The year with most messages was **{most_year}** with "
        f"**{report.tot_year.get(most_year)}** and the month was "
        f"**{most_msg_month_label:%B %Y}** with **{report.tot_month.get(most_month)}**"
    )

    @st.fragment
    def messages_count_charts():
        order = st.radio(
            "order",
            ["Date", "Messages"],
            label_visibility="collapsed",
            horizontal=True,
            key="order-1",
        )
        bar_chart = DoubleBarChart(report.tot_per_user, order == "Date", 1)
        bar_chart.build()
        st.altair_chart(bar_chart.chart, use_container_width=True)

        # bar chart | messages per month
        bar_chart = DoubleBarChart(report.tot_per_user, index=2)
        bar_chart.df["index"] = pd.to_datetime(bar_chart.df["index"], format="%m.%Y")
        name_sorting, dir_sorting = (
            "index" if order == "Date" else "Value",
            order == "Date",
        )
        bar_chart.df = bar_chart.df.sort_values(name_sorting, ascending=dir_sorting)
        bar_chart.df["index"] = bar_chart.df["index"].dt.strftime("%B %Y")
        bar_chart.build()
        st.altair_chart(bar_chart.chart, use_container_width=True)

    messages_count_charts()

    # ---- types
    st.subheader("Message types", divider="gray")
    most_type = sorted(report.types, key=lambda x: -report.types.get(x))[0]
    st.markdown(
        f"The most sent type of messages is :grey-background[{most_type}]. In the pie "
        "chart on the right, it is excluded for greater understandability."
    )
    # pie charts | messages type
    left_col, right_col = st.columns(2)
    types = {m_type: count for m_type, count in report.types.items()}
    pie_chart = PieChart(types)
    pie_chart.build()
    left_col.pyplot(pie_chart.fig, use_container_width=True)

    # pie charts | messages type (w/o best)
    types = {m_type: count for m_type, count in report.types.items()}
    types.pop(most_type, None)
    pie_chart = PieChart(types)
    pie_chart.build()
    right_col.pyplot(pie_chart.fig, use_container_width=True)

    # bar chart | types per user
    @st.fragment
    def types_charts():
        most_type_enable = st.checkbox(
            f"Show :gray-background[{most_type}] messages", True
        )
        excluded_types = not most_type_enable and most_type
        bar_chart = DoubleBarChart(report.types_per_user, exclude=excluded_types)
        bar_chart.build(stacked=False)
        st.altair_chart(bar_chart.chart, use_container_width=True)

    types_charts()

    # ---- lenghts
    st.subheader("Longest messages", divider="gray")
    st.markdown(
        f"The average message length is **{report.len_mean:.1f}** characters with a "
        f"standard deviation of **{report.len_std:.1f}** characters."
    )
    cols = st.columns(len(report.tot_per_user) + 1)
    cols[0].metric("Total characters", sum(report.len_chars))
    for i, (user, user_tot) in enumerate(report.lengths_per_user.items(), 1):
        cols[i % 3].metric(
            f"Mean :gray-background[**{user}**] characters",
            format(user_tot[2], ".1f"),
            delta_color="off",
        )
    st.write("The following are the **3 longest messages** in the chat")
    for _, who, _, message, date, *_ in report.lengths:
        with st.expander(
            f":gray-background[{who}] on **{date:%d %B %Y}** "
            f"wrote a **{len(message)}** characters long message"
        ):
            st.write(f":gray[{message}]")
    st.write("The following are the **longest messages** from all users")
    cols = st.columns(2)
    for i, (user, (messages, _, _, _)) in enumerate(report.lengths_per_user.items()):
        _, who, _, message, date, *_ = messages[0]
        with cols[i % 2].expander(
            f":gray-background[{who}] on **{date:%d %B %Y}** wrote "
            f"a **{len(message)}** characters long message"
        ):
            st.write(f":gray[{message}]")

    # ---- occurence | word
    st.subheader("Recurring words", divider="gray")
    most_recurring = list(report.words.items())
    most_recurring_word = most_recurring[0]
    plus50_occurence = len([word for word, count in most_recurring if count > 50])
    st.markdown(
        f"The word that recurs the most is **{most_recurring_word[0]}** which appears "
        f"**{most_recurring_word[1]}** times. There are **{plus50_occurence}** "
        f"words recurring more than 50 times."
    )
    cols = st.columns(3)
    for i, ((word, count), medal) in enumerate(zip(most_recurring[:3], MEDALS)):
        cols[i % 3].metric(
            f"{medal} :gray[| **{count}** occurences]",
            word,
            delta_color="off",
        )
    cols = st.columns(2)
    for i, (user, words) in enumerate(report.words_per_user.items()):
        word, count = list(words.items())[0]
        cols[i % 2].metric(
            f"Most used by :gray-background[{user}] :gray[| **{count}** occurences]",
            word,
            delta_color="off",
        )
        # table | words per user
        bar_chart = BarChart(words, order_x=False)
        cols[i % 2].dataframe(
            bar_chart.df.head(10),
            hide_index=True,
            use_container_width=True,
            column_config={"index": "Word", "Value": "Occurences"},
        )

    # search keyword
    @st.fragment
    def searching_keyword():
        word_search = st.selectbox("Search a keyword", words, None, placeholder="words")
        if word_search:
            occurences = Counter(
                msg[1] for msg in report.data if word_search in msg[3].lower()
            )
            cols = st.columns(len(occurences) + 1)
            cols[0].metric("Total occurences", sum(occurences.values()))
            for i, (user, occurence) in enumerate(occurences.items(), 1):
                cols[i % 3].metric(
                    f"Total :gray-background[**{user}**] occurences",
                    format(occurence),
                    delta_color="off",
                )

    searching_keyword()

    # ---- occurence | phrases
    st.subheader("Recurring phrases", divider="gray")
    most_recurring_2 = list(report.phrases_2.items())
    most_recurring_phrase2 = most_recurring_2[0]
    most_recurring_3 = list(report.phrases_3.items())
    most_recurring_phrase3 = most_recurring_3[0]
    plus50_occurence = len(
        [phrase for phrase, count in most_recurring_2 + most_recurring_3 if count > 50]
    )
    st.markdown(
        f"The 2-word phrase that recurs the most is **{most_recurring_phrase2[0]}** "
        f"which appears **{most_recurring_phrase2[1]}** times. The 3-word phrase that "
        f"recurs the most is **{most_recurring_phrase3[0]}** which appears "
        f"**{most_recurring_phrase3[1]}** times. There are **{plus50_occurence}** "
        f"words recurring more than 50 times."
    )
    cols = st.columns(3)
    for i, ((phrase, count), medal) in enumerate(zip(most_recurring_2[:3], MEDALS)):
        cols[i % 3].metric(
            f"{medal} :gray[| **{count}** occurences]",
            phrase,
            delta_color="off",
        )
    phrases_per_user = {
        user: report.phrases_2_per_user.get(user) | report.phrases_3_per_user.get(user)
        for user in report.phrases_2_per_user
    }
    phrases_per_user = {
        user: {
            phrase: phrases.get(phrase)
            for phrase in sorted(phrases, key=lambda x: phrases.get(x, 0))
        }
        for user, phrases in phrases_per_user.items()
    }
    cols = st.columns(2)
    for i, (user, phrases) in enumerate(phrases_per_user.items()):
        phrase, count = list(phrases.items())[-1]
        cols[i % 2].metric(
            f"Most used by :gray-background[{user}] :gray[| **{count}** occurences]",
            phrase,
            delta_color="off",
        )
        # table | phrases per user
        bar_chart = BarChart(phrases, order_x=False)
        cols[i % 2].dataframe(
            bar_chart.df.head(10),
            hide_index=True,
            use_container_width=True,
            column_config={"index": "Phrase", "Value": "Occurences"},
        )

    # ---- Emoji
    st.subheader("Emojis", divider="gray")
    most_recurring = list(report.emojis.items())
    most_recurring_emoji = most_recurring[0]
    plus50_occurence = len([emoji for emoji, count in most_recurring if count > 50])
    st.markdown(
        f"The emoji that recurs the most is **{most_recurring_emoji[0]}** which appears"
        f" **{most_recurring_emoji[1]}** times. There are **{plus50_occurence}** "
        f"emojis recurring more than 50 times."
    )
    cols = st.columns(3)
    for i, ((emoji, count), medal) in enumerate(zip(most_recurring[:3], MEDALS)):
        cols[i % 3].metric(
            f"{medal} :gray[| **{count}** occurences]",
            emoji,
            delta_color="off",
        )
    cols = st.columns(2)
    for i, (user, emojis) in enumerate(report.emoji_per_user.items()):
        if not emojis:
            continue
        emoji, count = list(emojis.items())[0]
        cols[i % 2].metric(
            f"Most used by :gray-background[{user}] :gray[| **{count}** occurences]",
            emoji,
            delta_color="off",
        )
        # table | emoji per user
        bar_chart = BarChart(emojis, order_x=False)
        cols[i % 2].dataframe(
            bar_chart.df.head(10),
            hide_index=True,
            use_container_width=True,
            column_config={"index": "Emoji", "Value": "Occurences"},
        )
    recurring_once = [emoji for emoji, count in most_recurring if count == 1]
    st.markdown(f"There are **{len(recurring_once)}** emojis recurring only once")
    st.subheader("".join(sorted(recurring_once, reverse=True)), anchor=False)

    # ---- sentiment
    st.subheader("Sentiment analysis", divider="gray")
    st.warning(
        "The [model used](https://huggingface.co/MilaNLProc/feel-it-italian-emotion) "
        "to study the sentiment and emotions of the messages has an accuracy of "
        "**73%**. The results may not be entirely true!",
        icon="⚠️",
    )
    positive_msg = report.sentiments.get("positive") / sum(
        report.sentiments.get(label) for label in report.sentiments if label != "-"
    )
    most_emotion = sorted(report.emotions, key=lambda x: -report.emotions.get(x))[0]
    most_emotion_perc = report.emotions.get(most_emotion) / sum(
        report.emotions.get(label) for label in report.emotions if label != "-"
    )
    st.markdown(
        f"Positive conversation messages are **{positive_msg:.0%}** of text messages. "
        f"The predominant emotion is **{most_emotion}** with "
        f"**{most_emotion_perc:.0%}** of text messages."
    )

    # bar chart | emotions per user
    bar_chart = DoubleBarChart(report.analysis_user, index=1, exclude="-")
    bar_chart.build(stacked=False)
    st.altair_chart(bar_chart.chart, use_container_width=True)

    # bar chart | sentiment per user
    bar_chart = DoubleBarChart(report.analysis_user, index=0, exclude="-", reverse=True)
    bar_chart.build(stacked=False)
    st.altair_chart(bar_chart.chart, use_container_width=True)

    # line chart | sentiment over time
    @st.fragment
    def sentiment_charts():
        st.subheader("Sentiment analysis over time")
        year_filter = st.selectbox(
            "Year",
            sorted(set([msg[4].year for msg in report.data])),
            None,
            placeholder="Filter by year",
            label_visibility="collapsed",
        )
        data = [m for m in report.data if not year_filter or m[4].year == year_filter]
        line_chart = LineChart(data)
        line_chart.build()
        st.altair_chart(line_chart.chart, use_container_width=True)

    sentiment_charts()

    # ---- footer
    st.divider()
    st.caption(
        "Designed with ♥️ by [Mortafix](https://moris.dev) with "
        "[Streamlit](https://streamlit.io). Check out the code [HERE]()."
    )


if __name__ == "__main__":
    app()
