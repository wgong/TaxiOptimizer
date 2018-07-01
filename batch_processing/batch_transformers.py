#import os
import sys
sys.path.append("./helpers/")

import json
import heapq
import helpers
import psycopg2
import pyspark


####################################################################

class BatchTransformer:
    """
    class that reads data from S3 bucket, processes it with Spark
    and saves the results into PostgreSQL database
    """

    def __init__(self, s3_configfile, schema_configfile, psql_configfile):
        """
        class constructor that initializes the instance according to the configurations
        of the S3 bucket, raw data and PostgreSQL table
        :type s3_configfile:     str        path to s3 config file
        :type schema_configfile: str        path to schema config file
        :type psql_configfile:   str        path to psql config file
        """
        self.s3_config   = helpers.parse_config(s3_configfile)
        self.schema      = helpers.parse_config(schema_configfile)
        self.psql_config = helpers.parse_config(psql_configfile)

        self.sc = pyspark.SparkContext.getOrCreate()
        self.sc.setLogLevel("ERROR")


    def read_from_s3(self):
        """
        reads files from s3 bucket defined by s3_configfile and creates Spark RDD
        """
        filenames = "s3a://{}/{}/{}".format(self.s3_config["BUCKET"],
                                            self.s3_config["FOLDER"],
                                            self.s3_config["RAW_DATA_FILE"])
        self.data = self.sc.textFile(filenames)


    def save_to_postgresql(self):
        """
        saves result of batch transformation to PostgreSQL database
        """
        sqlContext = pyspark.sql.SQLContext(self.sc)
        sql_data   = sqlContext.createDataFrame(self.data) # need to use Row

        options = "".join([".options(%s=self.psql_config[\"%s\"])" % (opt, opt) for opt in ["url",
                                                                                            "dbtable",
                                                                                            "driver",
                                                                                            "user",
                                                                                            "password",
                                                                                            "partitionColumn",
                                                                                            "lowerBound",
                                                                                            "upperBound",
                                                                                            "numPartitions"]])
        command = "sql_data.write.format(\"jdbc\").mode(\"%s\")%s.save()" % (self.psql_config["mode"], options)
        eval(command)


    def add_index_postgresql(self):
        """
        adds index to PostgreSQL table on column time_slot
        """
        conn_string = "host='%s' dbname='%s' user='%s' password='%s'" % (self.psql_config["host"],
                                                                         self.psql_config["dbname"],
                                                                         self.psql_config["user"],
                                                                         self.psql_config["password"])
        conn = psycopg2.connect(conn_string)
        cursor = conn.cursor()
        cursor.execute("CREATE INDEX ON %s (%s)" % (self.psql_config["dbtable"],
                                                    self.psql_config["partitionColumn"]))
        conn.commit()
        cursor.close()
        conn.close()


    def spark_transform(self):
        """
        transforms Spark RDD with raw data into RDD with cleaned data;
        adds block_id, sub_block_id and time_slot fields
        """
        schema = self.sc.broadcast(self.schema)
        self.data = (self.data
                           .map(lambda line: helpers.map_schema(line, schema.value))
                           .map(helpers.add_block_fields)
                           .map(helpers.add_time_slot_field)
                           .map(helpers.check_passengers)
                           .filter(lambda x: x is not None))


    def run(self):
        """
        executes the read from S3, transform by Spark and write to PostgreSQL database sequence
        """
        self.read_from_s3()
        self.spark_transform()
        self.save_to_postgresql()
        self.add_index_postgresql()



####################################################################

class TaxiBatchTransformer(BatchTransformer):
    """
    class that calculates the top-n pickup spots from historical data
    """

    def spark_transform(self):
        """
        transforms Spark RDD with raw data into the RDD that contains
        top-n pickup spots for each block and time slot
        """
        BatchTransformer.spark_transform(self)

        n = self.psql_config["topntosave"]

        # calculation of top-n spots for each block and time slot
        self.data = (self.data
                        .map(lambda x: ( (x["block_id"], x["time_slot"], x["sub_block_id"]), x["passengers"] ))
                        .reduceByKey(lambda x,y: x+y)
                        .map(lambda x: ( (x[0][0], x[0][1]), [(x[0][2], x[1])] ))
                        .reduceByKey(lambda x,y: x+y)
                        .mapValues(lambda vals: heapq.nlargest(n, vals, key=lambda x: x[1]))
                        .map(lambda x: {"block_id":          x[0][0],
                                        "time_slot":         x[0][1],
                                        "subblocks_psgcnt":  x[1]}))


        # recalculation of top-n, where for each key=(block_id, time_slot) top-n is calculated
        # based on top-n of (block_id, time_slot) and top-ns of (adjacent_block, time_slot+1)
        # from all adjacent blocks
        self.data = (self.data
                        .map(lambda x: ( (x["block_id"], x["time_slot"]), x["subblocks_psgcnt"] ))
                        .flatMap(lambda x: [x] + [ ( (bl, (x[0][1]-1) % 144), x[1] ) for bl in helpers.get_neighboring_blocks(x[0][0]) ] )
                        .reduceByKey(lambda x,y: x+y)
                        .mapValues(lambda vals: heapq.nlargest(n, vals, key=lambda x: x[1]))
                        .map(lambda x: {"block_latid":  x[0][0][0],
                                        "block_lonid":  x[0][0][1],
                                        "time_slot":    x[0][1],
                                        "longitude":    [helpers.determine_subblock_lonlat(el[0])[0] for el in x[1]],
                                        "latitude":     [helpers.determine_subblock_lonlat(el[0])[1] for el in x[1]],
                                        "passengers":   [el[1] for el in x[1]] } ))
