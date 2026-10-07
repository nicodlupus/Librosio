# importing necessary libs
import json
import csv

# reading json
with open("books.json", "r", encoding="utf-8") as file:
    data = json.load(file)
    
# retrieving the attribute names from the first object
records = data["books"]
attributes = records[0].keys()

# creating the CSV
with open("books.csv", "w", nealine="", encoding="utf-8") as file:
    # adding the attributes
    writer = csv.DictWriter(file, fieldnames=attributes)
    writer.writeheader()
    # adding the data to the csv
    writer.writerows(records)
    
print("CSV created successfully.")