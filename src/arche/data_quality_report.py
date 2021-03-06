from io import StringIO
import json
from typing import Optional


from arche.figures import graphs
from arche.figures import tables
from arche.quality_estimation_algorithm import generate_quality_estimation
from arche.readers.items import Items
from arche.readers.schema import Schema
from arche.report import Report
import arche.rules.duplicates as duplicate_rules
from arche.rules.garbage_symbols import garbage_symbols
import arche.rules.price as price_rules
from arche.tools import api
from arche.tools.s3 import upload_str_stream
from arche.tools.schema import JsonFields
import plotly


class DataQualityReport:
    def __init__(
        self, items: Items, schema: Schema, report: Report, bucket: Optional[str] = None
    ):
        """Prints a data quality report

        Args:
            items: an Items instance containing items data
            schema: a schema dict
        """
        self.schema = schema
        self.report = report
        self.figures = []
        self.appendix = self.create_appendix(self.schema)
        self.create_figures(items, items.dicts)
        self.plot_to_notebook()

        if bucket:
            self.save_report_to_bucket(
                project_id=items.key.split("/")[0],
                spider=items.job.metadata.get("spider"),
                bucket=bucket,
            )

    def create_figures(self, items, items_dicts):
        jf = JsonFields(self.schema)
        tagged_fields = jf.tagged
        no_of_validated_items = len(items.df.index)

        dup_items_result = duplicate_rules.check_items(items.df, tagged_fields)
        no_of_checked_duplicated_items = dup_items_result.items_count
        no_of_duplicated_items = dup_items_result.err_items_count

        dup_skus_result = duplicate_rules.check_uniqueness(items.df, tagged_fields)
        no_of_checked_skus_items = dup_skus_result.items_count
        no_of_duplicated_skus = dup_skus_result.err_items_count

        price_was_now_result = price_rules.compare_was_now(items.df, tagged_fields)
        no_of_price_warns = price_was_now_result.err_items_count
        no_of_checked_price_items = price_was_now_result.items_count

        garbage_symbols_result = garbage_symbols(items)

        crawlera_user = api.get_crawlera_user(items.job)
        no_of_validation_warnings = self.report.results.get(
            "JSON Schema Validation"
        ).get_errors_count()
        quality_estimation, field_accuracy = generate_quality_estimation(
            items.job,
            crawlera_user,
            no_of_validation_warnings,
            no_of_duplicated_items,
            no_of_checked_duplicated_items,
            no_of_duplicated_skus,
            no_of_checked_skus_items,
            no_of_price_warns,
            no_of_validated_items,
            tested=True,
            garbage_symbols=garbage_symbols_result,
        )

        cleaned_df = self.drop_service_columns(items.df)

        self.score_table(quality_estimation, field_accuracy)
        self.job_summary_table(items.job)
        self.rules_summary_table(
            cleaned_df,
            no_of_validation_warnings,
            tagged_fields.get("name_field", ""),
            tagged_fields.get("product_url_field", ""),
            no_of_checked_duplicated_items,
            no_of_duplicated_items,
            tagged_fields.get("unique", []),
            no_of_checked_skus_items,
            no_of_duplicated_skus,
            tagged_fields.get("product_price_field", ""),
            tagged_fields.get("product_price_was_field", ""),
            no_of_checked_price_items,
            no_of_price_warns,
            garbage_symbols=garbage_symbols_result,
        )
        self.scraped_fields_coverage(items.job.key, cleaned_df)
        self.coverage_by_categories(cleaned_df, tagged_fields)

    def plot_to_notebook(self):
        for fig in self.figures:
            plotly.offline.iplot(fig, show_link=False)

    def plot_html_to_stream(self):
        output = StringIO()
        output.write(
            '<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>\n'
        )
        for fig in self.figures:
            output.write(
                plotly.offline.plot(
                    fig, include_plotlyjs=False, output_type="div", show_link=False
                )
            )
            output.write("\n")
        output.write(self.appendix)
        return output

    def create_appendix(self, schema):
        output = StringIO()
        output.write("<h1>Appendix</h1>\n")
        output.write("<h2>Appendix A: The JSON Schema</h2>\n")
        output.write("<pre>")
        output.write(json.dumps(schema, ensure_ascii=False, indent=2))
        output.write("</pre>")
        contents = output.getvalue()
        output.close()
        return contents

    def save_report_to_bucket(self, project_id, spider, bucket):
        report_stream = self.plot_html_to_stream()
        path = f"reports/dqr/{project_id}/Data Quality Report - {spider}.html"
        self.url = upload_str_stream(bucket, path, report_stream)
        report_stream.close()
        print(self.url)

    def score_table(self, quality_estimation, field_accuracy):
        score_table = tables.score_table(quality_estimation, field_accuracy)
        self.figures.append(score_table)

    def job_summary_table(self, job):
        summary_table = tables.job_summary_table(job)
        self.figures.append(summary_table)

    def rules_summary_table(
        self,
        df,
        no_of_validation_warnings,
        name_field,
        url_field,
        no_of_checked_duplicated_items,
        no_of_duplicated_items,
        unique,
        no_of_checked_skus,
        no_of_duplicated_skus,
        price_field,
        price_was_field,
        no_of_checked_price_items,
        no_of_price_warns,
        **kwargs,
    ):

        table = tables.rules_summary_table(
            df,
            no_of_validation_warnings,
            name_field,
            url_field,
            no_of_checked_duplicated_items,
            no_of_duplicated_items,
            unique,
            no_of_checked_skus,
            no_of_duplicated_skus,
            price_field,
            price_was_field,
            no_of_checked_price_items,
            no_of_price_warns,
            **kwargs,
        )
        self.figures.append(table)

    def scraped_fields_coverage(self, job, df):
        sfc = graphs.scraped_fields_coverage(job, df)
        self.figures.append(sfc)

    def scraped_items_history(self, job_no, job_numbers, date_items):
        sih = graphs.scraped_items_history(job_no, job_numbers, date_items)
        self.figures.append(sih)

    def coverage_by_categories(self, df, tagged_fields):
        """Makes tables which show the number of items per category,
        set up with a category tag

        Args:
            df: a dataframe of items
            tagged_fields: a dict of tags
        """
        category_fields = tagged_fields.get("category", list())
        product_url_fields = tagged_fields.get("product_url_field")

        for category_field in category_fields:
            cat_table = tables.coverage_by_categories(
                category_field, df, product_url_fields
            )
            if cat_table:
                self.figures.append(cat_table)

    def drop_service_columns(self, df):
        service_columns = ["_key", "_type", "_cached_page_id", "_validation"]
        found_columns = [cl for cl in service_columns if cl in df.columns]
        return df.drop(columns=found_columns)
