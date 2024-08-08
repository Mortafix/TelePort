import altair as alt
import matplotlib.pyplot as plt
import pandas as pd


class PieChart:
    def __init__(self, weights):
        self.size = (12, 4)
        self.data = {
            user: value
            for user in sorted(weights, key=lambda x: weights.get(x), reverse=True)
            if (value := weights.get(user))
        }
        self.labels = list(self.data)
        self.total = sum(self.data.values())

    def build(self):
        fig, ax = plt.subplots(figsize=self.size)
        self.fig = fig
        fig.patch.set_alpha(0)
        data_weights = list(self.data.values())
        wedges, _ = ax.pie(data_weights, startangle=90, counterclock=False)
        plt.setp(wedges, width=0.2)
        # legend
        ax.legend(
            wedges,
            [f"{lab} ({self.data.get(lab) / self.total:.2%})" for lab in self.labels],
            loc="center left",
            bbox_to_anchor=(1, 0, 0.5, 1),
        )


class DoubleBarChart:
    def __init__(self, data, order_x=True, index=None, reverse=False, exclude=None):
        self.chart_data = {
            user: {
                label: data.get(label, 0)
                for label in sorted(
                    data,
                    key=lambda x: x
                    if order_x
                    else -sum([data.get(x, 0) for values in data.values()]),
                    reverse=reverse,
                )
                if not exclude or label not in exclude
            }
            for user, values in data.items()
            if (data := values[index] if index is not None else values) is not None
        }
        self.df = (
            pd.DataFrame(self.chart_data)
            .reset_index()
            .melt(id_vars="index", var_name="Name", value_name="Value")
        )

    def build(self, stacked=True, limit=None):
        encoding = {
            "x": alt.X("index:O", sort=list(self.chart_data), title=None),
            "y": alt.Y("Value:Q", title=None),
            "color": alt.Color(
                "Name:N", legend=alt.Legend(orient="bottom", title=None)
            ),
        }
        if not stacked:
            encoding["xOffset"] = alt.XOffset("Name:N")
        self.chart = alt.Chart(self.df.head(limit)).mark_bar().encode(**encoding)


class BarChart:
    def __init__(self, data, order_x=True, index=None, reverse=False, exclude=None):
        data = data[index] if index is not None else data
        self.chart_data = {
            label: data.get(label, 0)
            for label in sorted(
                data,
                key=lambda x: x if order_x else -data.get(x, 0),
                reverse=reverse,
            )
            if not exclude or label not in exclude
        }
        self.df = pd.DataFrame(
            list(self.chart_data.items()), columns=["index", "Value"]
        )

    def build(self, limit=None):
        encoding = {
            "x": alt.X("index:O", sort=list(self.chart_data), title=None),
            "y": alt.Y("Value:Q", title=None),
        }
        self.chart = alt.Chart(self.df.head(limit)).mark_bar().encode(**encoding)


class LineChart:
    def __init__(self, data):
        df = pd.DataFrame(data)[[4, 6]]
        df.columns = ["date", "sentiment"]
        df["date"] = pd.to_datetime(df["date"]).dt.date
        # group by day and sentiment > diff of positive & negative
        df_sent = df.groupby(["date", "sentiment"]).size().unstack(fill_value=0)
        df_sent["ratio"] = df_sent["positive"] - df_sent["negative"]
        df_ratio = df_sent.reset_index()
        self.df = df_ratio

    def build(self):
        encoding = {
            "x": alt.X("date:T", title=None),
            "y": alt.Y("ratio:Q", title=None),
            "color": alt.condition(
                alt.datum.ratio > 0, alt.value("green"), alt.value("red")
            ),
        }
        self.chart = alt.Chart(self.df).mark_bar().encode(**encoding)
