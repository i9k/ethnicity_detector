import pandas as pd
import json

from datetime import datetime, timedelta
import time
import arrow

from collections import defaultdict, Counter
from unidecode import unidecode

from string import ascii_lowercase

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.orm import sessionmaker

# for sending an email notification:
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import schedule

import pymssql

import numpy as np

from ethnicitydetector import EthnicityDetector

import subprocess
import os

from functools import wraps

def timer(func):

	@wraps(func)  # to preserve finction's metadata
	def wrapper(*args, **kwargs):
		t_start = time.time()
		res = func(*args, **kwargs)
		print("f: {} # elapsed time: {:.0f} m {:.0f}s".format(func.__name__.upper(), *divmod(time.time() - t_start, 60)))
		return res

	return wrapper

class TableHandler(object):
	
	"""
	Class to connect to tables and get or upload stuff from/to tables
	"""
	def __init__(self, server, user, port, user_pwd, db_name,
					src_table='[DWSales].[dbo].[tbl_LotusCustomer]', 
						target_table='[TEGA].[dbo].[CustomerEthnicities8]'):
		
		self.CHNK = 20000  # process ethnicities by CHNK (in rows)
		self.TMP_NEW_CUSTOMER_TBLE = 'TEGA.dbo.tempNewCIDs'
		self.TMP_NEW_CUSTOMER_FILE = "temp_new_cids.csv"
		self.SRC_TABLE = src_table   # where we take customer id and name from 
		self.TARGET_TABLE = target_table
		self.TARGET_TABLE_FORMAT = "(CustomerID nvarchar(20), Ethnicity nvarchar(50), AssignedOn nvarchar(10))"

		self._ENGINE = sa.create_engine('mssql+pymssql://{}:{}@{}:{}/{}'.format(user, user_pwd, server, port, db_name))
		Ses = sessionmaker(bind=self._ENGINE, autocommit=True)
		self._SESSION = Ses()
		
		# temp table to upload detected ethnicities to
		self.TEMP_TABLE = "TEGA.dbo.tmpEthn"

		self.TODAY_SYD = arrow.utcnow().to('Australia/Sydney').format('DD-MM-YYYY')
		self.LAST_SEVEN_DAYS = " ".join(['BETWEEN', "'" + (datetime.now() + timedelta(days=-6)).strftime("%Y%m%d") + "'", "AND", "'" +
		datetime.now().strftime("%Y%m%d") + "'"])
		
		self.QRY_TIMESPAN = {"last_7_days": 
							{"descr": "within last seven days, " + self.LAST_SEVEN_DAYS.lower(),
							"qry": "((([ModifiedDate] >= [CreatedDate]) AND ([ModifiedDate] {})) OR ([CreatedDate] {})) AND ([CustomerListID] = 2)".format(*[self.LAST_SEVEN_DAYS]*2)},
					"before_today":
					{"descr": "before today",
					"qry": "((([ModifiedDate] >= [CreatedDate]) AND ([ModifiedDate] <= {})) OR ([CreatedDate] <= {}) ) AND ([CustomerListID] = 2)".format(*["'" + (datetime.now() +
						timedelta(days=-1)).strftime("%Y%m%d") + "'"]*2)}}  

		# initialise a data frame with detected ethnicities now so that we can append to it
		self._detected_ethnicities = pd.DataFrame()

		# activate ethnicity detector instance
		self.ed = EthnicityDetector()

		# vectorized function from ed to actually detect ethnicities (when applied to an array)
		self.vf = np.vectorize(self.ed.get_ethnicity)

		# bcp options
		self.BCP_OPTIONS = {"full_path": "bcp", 
								"format_file": "ethnicities.fmt", 
									"temp_csv_file": "tmp_ethns.csv", 
										"server": server}

 
	# wrapper around pandas to_sql

	@timer
	def  _push_to_sql(self, df_upload, into_table, eng):
	   
		df_upload.to_sql(into_table, eng, if_exists='append', index=False, dtype={"CustomerID": sa.types.String(length=20),
																			"Ethnicity": sa.types.String(length=50),
																			"AssignedOn": sa.types.String(length=10)},
																			chunksize=None if len(df_upload) <= 100000 else 100000)

	def get_array_ethnicity(self, b):  

		"""
		IN: numpy array b that has two columns, oned contains customer id and another a full name
		OUT: numpy array with teo columns: customer id and ethnicity
		
		!NOTE: there will be 'None' where no ethnicityhas been detected 
		"""
	
		ets = self.vf(b[:,-1])  # we assume that the second column contains full names

		stk = np.hstack((b[:,0].reshape(b.shape[0],1), ets.reshape(b.shape[0],1)))
	
		return stk

	# @timer
	# def get_ethnicities_parallel(self):

	# 	"""
	# 	apply get_array_ethnicity to a number of dataframe chunks in parallel and then gather the results
	# 	"""

	# 	print("identifying ethnicities in parallel...")

	# 	AVAIL_CPUS = multiprocessing.cpu_count()

	# 	pool = multiprocessing.Pool(AVAIL_CPUS)

	# 	self._detected_ethnicities = pd.DataFrame(np.vstack(pool.map(self.get_array_ethnicity, 
	# 									np.array_split(self._CUST_TO_CHECK.values, AVAIL_CPUS))),
	# 			   columns=["CustomerID", "Ethnicity"], dtype=str).query('len(Ethnicity) > 5')

	# 	pool.close()
	# 	pool.join()

	# 	return self

	@timer
	def get_ethnicities(self):
		"""
		simply apply ethnicity detector on a data frame column with full names
		"""

		for i, c in enumerate(pd.read_csv(self.TMP_NEW_CUSTOMER_FILE, sep='\t', dtype=str, error_bad_lines=False, header=None, chunksize=self.CHNK)):

			c['Ethnicity'] = c[1].apply(self.ed.get_ethnicity)
			c = c[c.Ethnicity.isin(self.ed.ETHNICITY_LIST)]
			c = c.rename(columns={0: "FullName"})
			c = c.drop(1, axis=1)

			self._detected_ethnicities = pd.concat([self._detected_ethnicities, c])

			print('ethnicity: processed {} customer ids...'.format((i+1)*self.CHNK))

		self._detected_ethnicities["AssignedOn"] = self.TODAY_SYD

		return self

	def _recreate_table(self, table_name, table_fmt, if_exists):

		try:
			self._SESSION.execute(" ".join(["CREATE TABLE", table_name, table_fmt]))
		except exc.OperationalError:  # this comes up if table elready exists
			# means drop didn't work
			if if_exists == 'create_new':
				self._SESSION.execute(" ".join(["DROP TABLE", table_name]))
				self._SESSION.execute(" ".join(["CREATE TABLE", table_name, table_fmt]))
			else:
				pass

		print('re-created table {}'.format(table_name))

	def _newcids_to_temp_table(self):
		"""
		find out which customer ids are of interest to us (and hence to be checked for ethnicity) and then collect these along with 
		the corresponding names in a temporary table
		"""
		nrs = self._SESSION.execute(" ".join(["SELECT COUNT (*) FROM", self.SRC_TABLE,
										 "WHERE", self.QRY_TIMESPAN["before_today"]["qry"]])).fetchone()[0]
		print('customer ids to check for ethnicity: {}'.format(nrs))
		print('running bcp to collect..')

		self._recreate_table(self.TMP_NEW_CUSTOMER_TBLE, '(CustomerID int, full_name nvarchar(50))', 'create_new')

		print('now selecting into that table...')
		qr = "INSERT INTO " + self.TMP_NEW_CUSTOMER_TBLE + " SELECT [CustomerID], SUBSTRING(ISNULL([FirstName],'') + ' ' + ISNULL([MiddleName],'') + ' ' + ISNULL([LastName],''),1,50) as [full_name] FROM " + self.SRC_TABLE + " WHERE " + self.QRY_TIMESPAN["before_today"]["qry"]
		#print(qr)

		self._SESSION.execute(qr)
		
		# now that this temp table is already there, download it using bcp
		subprocess.run("bcp " + self.TMP_NEW_CUSTOMER_TBLE + " out " + self.TMP_NEW_CUSTOMER_FILE + " -c -C 65001 -T -S " + self.BCP_OPTIONS['server'])
		print('created local temporary file with new customer ids...')


	@timer
	def proc_new_customers(self):
		
		"""
		get all new customers of interest from Lotus and p[ut them into a data frame]
		"""

		# create a local csv
		self._newcids_to_temp_table()

		#self.get_ethnicities_parallel()
		self.get_ethnicities()

		print("found {} ethnicities".format(len(self._detected_ethnicities)))

		return self
	
	@timer
	def update_ethnicity_table(self):

		if len(self._detected_ethnicities) < 1:

			print("[WARNING]: no new ethnicities to upload!")

		else:

			t_start = time.time()

			print("uploading ethnicities to temporary table {}...".format(self.TEMP_TABLE))

			self._recreate_table(self.TEMP_TABLE, self.TARGET_TABLE_FORMAT, 'create_new')
			
			# create a format file for bcp (from the temporary table, it's the same as format for target table)
			subprocess.run("bcp " + self.TEMP_TABLE + ' format nul -f ' + self.BCP_OPTIONS["format_file"] + '-n -T -S ' + self.BCP_OPTIONS["server"])
			self._detected_ethnicities.to_csv(self.BCP_OPTIONS["temp_csv_file"])
			# now upload the csv file we have just created to SQL server usong bcp and the format file
			subprocess.run('bcp ' + self.TEMP_TABLE + " in " + self.BCP_OPTIONS["temp_csv_file"]+ '-t, -T -S ' + self.BCP_OPTIONS["server"] + ' -f ' + self.BCP_OPTIONS["format_file"])

			ROWS_TMP = self._SESSION.execute("SELECT COUNT (*) FROM {};".format(self.TEMP_TABLE)).fetchone()[0]
				
			print("placed new ethnicities in a temporary table with {} rows [{:.0f} min {:.0f} sec]...".format(ROWS_TMP, *divmod(time.time() - t_start, 60)))
			
			# now append the ethnicities in temporary table to the target table (replace those already there)

			self._recreate_table(self.TARGET_TABLE, self.TARGET_TABLE_FORMAT, 'do_nothing')

			self._SESSION.execute("DELETE FROM " + self.TARGET_TABLE + " WHERE CustomerID in (SELECT CustomerID FROM {});".format(self.TEMP_TABLE))
			
			self._SESSION.execute("INSERT INTO " + self.TARGET_TABLE + " SELECT * FROM " + self.TEMP_TABLE)
			print("update complete [{:.0f} min {:.0f} sec]...".format(*divmod(time.time() - t_start, 60)))
	
	
	def send_email(self):
		
		sender_email, sender_pwd, smtp_server, smpt_port, recep_emails = [line.split("=")[-1].strip() 
									for line in open("config/email.cnf", "r").readlines() if line.strip()]
		
		msg = MIMEMultipart()   
		
		msg['From'] = sender_email
		msg['To'] = recep_emails
		msg['Subject'] = 'ethnicities: customers created or modified {}'.format(self.QRY_TIMESPAN["before_today"]["descr"])
		
		dsample = pd.DataFrame()

		for k, v in Counter(self._detected_ethnicities['Ethnicity']).items():
			this_ethnicity = self._detected_ethnicities[self._detected_ethnicities.Ethnicity == k]
			ns = 3 if len(this_ethnicity) > 2 else 1
			dsample = pd.concat([dsample, this_ethnicity.sample(n=ns)])

		st_summary  = "-- new ethnic customer ids captured:\n\n" + \
				"".join(["{}: {}\n".format(ks.upper(), vs) for ks, vs in sorted([(k,v) 
					for k, v in Counter(self._detected_ethnicities['Ethnicity']).items()], key=lambda x: x[1], reverse=True)])
		
		msg.attach(MIMEText(st_summary+ "\n-- sample:\n\n" + dsample.loc[:,["CustomerID", "FullName", "Ethnicity"]].to_string(index=False, justify="left",
			formatters=[lambda _: "{:<12}".format(str(_).strip()), lambda _: "{:<30}".format(str(_).strip()), lambda _: "{:<20}".format(str(_).strip())]), 'plain'))
		server = smtplib.SMTP(smtp_server, smpt_port)
		server.starttls()
		print('sending email notification...', end='')
		server.login(sender_email, sender_pwd)
		server.sendmail(sender_email, [email.strip() for email in recep_emails.split(";")], msg.as_string())
		print('ok')
		server.quit()

if __name__ == '__main__':

	tc = TableHandler(**json.load(open("config/conn-02.ini", "r")))

	def job():
		
		tc.proc_new_customers()
		
		tc.update_ethnicity_table()
		
		tc.send_email()

	schedule.every().day.at('17:10').do(job)
	
	while True:

		schedule.run_pending()
