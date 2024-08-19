# Databricks notebook source
#dbutils.widgets.dropdown("force_refresh_automl", "true", ["false", "true"], "Restart AutoML run")

# COMMAND ----------

# MAGIC %md
# MAGIC # Churn Prediction Feature Engineering
# MAGIC Our first step is to analyze the data and build the features we'll use to train our model. Let's see how this can be done.
# MAGIC
# MAGIC <img src="https://github.com/QuentinAmbard/databricks-demo/raw/main/product_demos/mlops-end2end-flow-1.png" width="1200">
# MAGIC
# MAGIC <!-- Collect usage data (view). Remove it to disable collection. View README for more details.  -->
# MAGIC <img width="1px" src="https://www.google-analytics.com/collect?v=1&gtm=GTM-NKQ8TT7&tid=UA-163989034-1&cid=555&aip=1&t=event&ec=field_demos&ea=display&dp=%2F42_field_demos%2Ffeatures%2Fmlops%2F02_feature_prep&dt=MLOPS">
# MAGIC <!-- [metadata={"description":"MLOps end2end workflow: Feature engineering",
# MAGIC  "authors":["quentin.ambard@databricks.com"],
# MAGIC  "db_resources":{},
# MAGIC   "search_tags":{"vertical": "retail", "step": "Data Engineering", "components": ["feature store"]},
# MAGIC                  "canonicalUrl": {"AWS": "", "Azure": "", "GCP": ""}}] -->

# COMMAND ----------

# MAGIC %pip install --quiet mlflow==2.14.3
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ../_resources/00-setup $reset_all_data=false

# COMMAND ----------

# MAGIC %md
# MAGIC ## Exploratory Data Anaylsis
# MAGIC To get a feel of the data, what needs cleaning, pre-processing etc.
# MAGIC - **Use Databricks's native visualization tools**
# MAGIC - Bring your own visualization library of choice (i.e. seaborn, plotly)

# COMMAND ----------

# DBTITLE 1,Read in Bronze Delta table using Spark
# Read into Spark
telcoDF = spark.read.table(bronze_table_name)
display(telcoDF)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Define Featurization Logic(s) for BATCH feature computation
# MAGIC
# MAGIC 1. Compute number of active services
# MAGIC 2. Clean-up names and manual mapping
# MAGIC
# MAGIC _This can also work for streaming based features_

# COMMAND ----------

# MAGIC %md
# MAGIC ### Using PandasUDF and PySpark
# MAGIC To scale pandas analytics on a spark dataframe

# COMMAND ----------

primary_key = "customer_id"
timestamp_col ="transaction_ts"
label_col = "churn"
feature_table_name = "churn_feature_table"

# COMMAND ----------

from pyspark.sql import DataFrame as SparkDataFrame
from pyspark.sql.functions import pandas_udf, col, when, lit

def compute_service_features(inputDF: SparkDataFrame) -> SparkDataFrame:
  """
  Count number of optional services enabled, like streaming TV
  """

  # Create pandas UDF function
  @pandas_udf('double')
  def num_optional_services(*cols):
    """Nested helper function to count number of optional services in a pandas dataframe"""
    return sum(map(lambda s: (s == "Yes").astype('double'), cols))

  return inputDF.\
    withColumn("num_optional_services",
        num_optional_services("online_security", "online_backup", "device_protection", "tech_support", "streaming_tv", "streaming_movies"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Using Pandas On Spark API
# MAGIC
# MAGIC Because our Data Scientist team is familiar with Pandas, we'll use the [pandas on spark API](https://spark.apache.org/docs/latest/api/python/reference/pyspark.pandas/index.html) to scale `pandas` code. The Pandas instructions will be converted in the spark engine under the hood and distributed at scale.
# MAGIC
# MAGIC *Note: Starting from `spark 3.2`, koalas is builtin and we can get an Pandas Dataframe using `pandas_api()`.*

# COMMAND ----------

# DBTITLE 1,Define featurization function
def clean_churn_features(dataDF: SparkDataFrame) -> SparkDataFrame:
  """
  Simple cleaning function leveraging pandas API
  """

  # Convert to pandas on spark dataframe
  data_psdf = dataDF.pandas_api()

  # Convert some columns
  data_psdf["senior_citizen"] = data_psdf["senior_citizen"].map({1 : "Yes", 0 : "No"})
  data_psdf = data_psdf.astype({"total_charges": "double", "senior_citizen": "string"})

  # Fill some missing numerical values with 0
  data_psdf = data_psdf.fillna({"tenure": 0.0})
  data_psdf = data_psdf.fillna({"monthly_charges": 0.0})
  data_psdf = data_psdf.fillna({"total_charges": 0.0})

  # Add/Force semantic data types for specific colums (to facilitate autoML)
  data_cleanDF = data_psdf.to_spark()
  data_cleanDF = data_cleanDF.withMetadata(primary_key, {"spark.contentAnnotation.semanticType":"native"})
  data_cleanDF = data_cleanDF.withMetadata("num_optional_services", {"spark.contentAnnotation.semanticType":"numeric"})

  return data_cleanDF

# COMMAND ----------

# MAGIC %md-sandbox
# MAGIC
# MAGIC ## Compute & Write to Feature Store
# MAGIC
# MAGIC <img src="https://github.com/QuentinAmbard/databricks-demo/raw/main/product_demos/mlops-end2end-flow-feature-store.png" style="float:right" width="500" />
# MAGIC
# MAGIC Once our features are ready, we'll save them in Databricks Feature Store. Under the hood, features store are backed by a Delta Lake table.
# MAGIC
# MAGIC This will allow discoverability and reusability of our feature accross our organization, increasing team efficiency.
# MAGIC
# MAGIC Feature store will bring traceability and governance in our deployment, knowing which model is dependent of which set of features.
# MAGIC
# MAGIC Make sure you're using the "Machine Learning" menu to have access to your feature store using the UI.

# COMMAND ----------

# DBTITLE 1,Compute Churn Features and append a timestamp
from datetime import datetime

# Add current scoring timestamp
this_time = (datetime.now()).timestamp()
churn_features_n_predsDF = clean_churn_features(compute_service_features(telcoDF)) \
                            .withColumn(timestamp_col, lit(this_time).cast("timestamp"))

display(churn_features_n_predsDF)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Extract ground-truth labels in a separate table to avoid label leakage

# COMMAND ----------

# DBTITLE 1,Extract ground-truth labels in a separate table and drop from Feature table
# Extract labels in separate table before pushing to Feature Store to avoid label leakage
# Also specify train-val-test split in the label table

import pyspark.sql.functions as F

# Specify train-val-test split
train_ratio, val_ratio, test_ratio = 0.7, 0.2, 0.1

churn_features_n_predsDF.select(primary_key, timestamp_col, label_col) \
                        .withColumn("random", F.rand(seed=42)) \
                        .withColumn("split",
                                    F.when(F.col("random") < train_ratio, "train")
                                    .when(F.col("random") < train_ratio + val_ratio, "validate")
                                    .otherwise("test")) \
                        .drop("random") \
                        .write.format("delta") \
                        .mode("overwrite").option("overwriteSchema", "true") \
                        .saveAsTable(f"{catalog}.{db}.{advanced_label_table_name}")

churn_featuresDF = churn_features_n_predsDF.drop(label_col)

# COMMAND ----------

# MAGIC %md
# MAGIC Add primary keys constraints to labels table for feature lookup

# COMMAND ----------

spark.sql(f"ALTER TABLE {catalog}.{db}.{advanced_label_table_name } ALTER COLUMN {primary_key} SET NOT NULL")
spark.sql(f"ALTER TABLE {catalog}.{db}.{advanced_label_table_name } ALTER COLUMN {timestamp_col} SET NOT NULL")
spark.sql(f"ALTER TABLE {catalog}.{db}.{advanced_label_table_name } ADD CONSTRAINT {advanced_label_table_name }_pk PRIMARY KEY({primary_key}, {timestamp_col})")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Write the feature table to Unity Catalog
# MAGIC
# MAGIC With Unity Catalog, any Delta table with a primary key constraint can be used as a feature table. It is used as the offline store. It's that easy.
# MAGIC
# MAGIC Time series feature tables have an additional primary key on the time column.
# MAGIC
# MAGIC After the table is created, you can write data to it like other Delta tables, and use it as a feature table.
# MAGIC
# MAGIC Here, we demonstrate creating the feature table using the `FeatureEngineeringClient` API. You can also easily create it using SQL:
# MAGIC
# MAGIC <br>
# MAGIC
# MAGIC ```
# MAGIC CREATE TABLE {catalog}.{db}.{feature_table_name} (
# MAGIC   {primary_key} int NOT NULL,
# MAGIC   {timestamp_col} timestamp NOT NULL,
# MAGIC   feat1 long,
# MAGIC   feat2 varchar(100),
# MAGIC   CONSTRAINT customer_features_pk PRIMARY KEY ({primary_key}, {timestamp_col} TIMESERIES)
# MAGIC );
# MAGIC ```
# MAGIC

# COMMAND ----------

# DBTITLE 1,Import Feature Store Client
from databricks.feature_engineering import FeatureEngineeringClient

fe = FeatureEngineeringClient()

# COMMAND ----------

# DBTITLE 1,Drop any existing online table (optional)
from pprint import pprint
from databricks.sdk import WorkspaceClient


# Create workspace client
w = WorkspaceClient()

try:

  online_table_specs = w.online_tables.get(f"{catalog}.{db}.{feature_table_name}_online_table")
  
  # Drop existing online feature table
  w.online_tables.delete(f"{catalog}.{db}.{feature_table_name}_online_table")
  print(f"Dropping online feature table: {catalog}.{db}.{feature_table_name}_online_table")

except Exception as e:
  pprint(e)

# COMMAND ----------

# DBTITLE 1,Drop feature table if it already exists (optional)

try:

  # Drop existing table from Feature Store
  fe.drop_table(name=f"{catalog}.{db}.{feature_table_name}")

  # Delete underyling delta tables
  spark.sql(f"DROP TABLE IF EXISTS {catalog}.{db}.{feature_table_name}")
  print(f"Dropping Feature Table {catalog}.{db}.{feature_table_name}")


except ValueError as ve:
  pass
  print(f"Feature Table {catalog}.{db}.{feature_table_name} doesn't exist")

# COMMAND ----------

churn_feature_table = fe.create_table(
  name=feature_table_name, # f"{catalog}.{dbName}.{feature_table_name}"
  primary_keys=[primary_key, timestamp_col],
  schema=churn_featuresDF.schema,
  timeseries_columns=timestamp_col,
  description=f"These features are derived from the {catalog}.{db}.{bronze_table_name} table in the lakehouse. We created service features, cleaned up their names.  No aggregations were performed. [Warning: This table doesn't store the ground-truth and now can be used with AutoML's Feature Store integration"
)

# COMMAND ----------

# DBTITLE 1,Write feature values to Feature Store
fe.write_table(
  name=f"{catalog}.{db}.{feature_table_name}",
  df=churn_featuresDF, # can be a streaming dataframe as well
  mode='merge' #'merge'/'overwrite' which supports schema evolution
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Define Featurization Logic for on-demand feature functions
# MAGIC
# MAGIC For features that can needs to be calculated on-demand see more info here ([AWS](https://docs.databricks.com/en/machine-learning/feature-store/on-demand-features.html)|[Azure](https://learn.microsoft.com/en-us/azure/databricks/machine-learning/feature-store/on-demand-features)) These can be used in both batch inference and online inference.

# COMMAND ----------


# Define the Python UDF

function_def = f"""
  CREATE OR REPLACE FUNCTION {catalog}.{db}.avg_price_increase(monthly_charges_in DOUBLE, tenure_in DOUBLE, total_charges_in DOUBLE)
  RETURNS FLOAT
  LANGUAGE PYTHON
  COMMENT "[Feature Function] Calculate potential average price increase for tenured customers based on last monthly charges and updated tenure"
  AS $$
  if tenure_in > 0:
    return monthly_charges_in - total_charges_in/tenure_in
  else:
    return 0
  $$
"""

spark.sql(function_def)

# COMMAND ----------

# MAGIC %sql
# MAGIC DESCRIBE FUNCTION avg_price_increase
# MAGIC ;

# COMMAND ----------

# MAGIC %md-sandbox
# MAGIC
# MAGIC ## Accelerating Churn model creation using Databricks Auto-ML
# MAGIC ### A glass-box solution that empowers data teams without taking away control
# MAGIC
# MAGIC Databricks simplify model creation and MLOps. However, bootstraping new ML projects can still be long and inefficient.
# MAGIC
# MAGIC Instead of creating the same boilerplate for each new project, Databricks Auto-ML can automatically generate state of the art models for Classifications, regression, and forecast.
# MAGIC
# MAGIC
# MAGIC <img width="1000" src="https://github.com/QuentinAmbard/databricks-demo/raw/main/retail/resources/images/auto-ml-full.png"/>
# MAGIC
# MAGIC <img style="float: right" width="600" src="https://github.com/QuentinAmbard/databricks-demo/raw/main/retail/resources/images/churn-auto-ml.png"/>
# MAGIC
# MAGIC Models can be directly deployed, or instead leverage generated notebooks to boostrap projects with best-practices, saving you weeks of efforts.
# MAGIC
# MAGIC ### Using Databricks Auto ML with our Churn dataset
# MAGIC
# MAGIC Auto ML is available in the "Machine Learning" space. All we have to do is start a new Auto-ML experimentation and select the table containint the ground-truth labels (i.e. `dbdemos.schema.churn_label_table`) and join it with the features in the feature table (i.e. `dbdemos.schema.churn_feature_table`)
# MAGIC
# MAGIC Our prediction target is the `churn` column.
# MAGIC
# MAGIC Click on Start, and Databricks will do the rest.
# MAGIC
# MAGIC While this is done using the UI, you can also leverage the [python API](https://docs.databricks.com/applications/machine-learning/automl.html#automl-python-api-1)
# MAGIC
# MAGIC #### Join/Use features directly from the Feature Store from the [UI](https://docs.databricks.com/machine-learning/automl/train-ml-model-automl-ui.html#use-existing-feature-tables-from-databricks-feature-store) or [python API]()
# MAGIC * Select the table containing the ground-truth labels (i.e. `dbdemos.schema.churn_label_table`)
# MAGIC * Join remaining features from the feature table (i.e. `dbdemos.schema.churn_feature_table`)
# MAGIC
# MAGIC Refer to the __Quickstart__ version of this demo for an example of AutoML in action.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Using the generated notebook to build our model
# MAGIC
# MAGIC Next step: [Explore the modfied version of the notebook generated from Auto-ML]($./02_automl_champion)
# MAGIC
# MAGIC TODO: To check - may not be able to simply register an AutoML model
# MAGIC
# MAGIC **Note:**
# MAGIC For demo purposes, run the above notebook OR create and register a new version of the model from your autoML experiment and label/alias the model as "Champion"
