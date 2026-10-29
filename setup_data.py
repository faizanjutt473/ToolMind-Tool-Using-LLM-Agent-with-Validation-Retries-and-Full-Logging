import sqlite3, csv, random, os

os.makedirs("data", exist_ok=True)
random.seed(42)

# 1. SQLite database
conn = sqlite3.connect("data/company.db")
c = conn.cursor()
c.execute("DROP TABLE IF EXISTS employees")
c.execute("DROP TABLE IF EXISTS sales")
c.execute("CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, department TEXT, salary INTEGER, city TEXT)")
c.execute("CREATE TABLE sales(id INTEGER PRIMARY KEY, product TEXT, amount INTEGER, sale_date TEXT)")

names = ["Ali","Ahmed","Sara","Hina","Usman","Bilal","Ayesha","Zain","Fatima","Hamza","Noor","Imran","Maryam","Talha","Sana"]
depts = ["Engineering","Sales","HR","Finance","Marketing"]
cities = ["Lahore","Karachi","Islamabad","Faisalabad","Jhang"]
for i, n in enumerate(names, 1):
    c.execute("INSERT INTO employees VALUES (?,?,?,?,?)",
              (i, n, random.choice(depts), random.randint(60, 400) * 1000, random.choice(cities)))

products = ["Laptop","Mouse","Keyboard","Monitor","Headphones"]
for i in range(1, 41):
    c.execute("INSERT INTO sales VALUES (?,?,?,?)",
              (i, random.choice(products), random.randint(1, 50) * 1000,
               "2026-%02d-%02d" % (random.randint(1, 9), random.randint(1, 28))))
conn.commit()
conn.close()

# 2. CSV file
with open("data/sales.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["product", "quantity", "price", "region"])
    for _ in range(30):
        w.writerow([random.choice(products), random.randint(1, 20),
                    random.randint(500, 150000), random.choice(["North","South","Central"])])

# 3. Text file
with open("data/notes.txt", "w") as f:
    f.write("Company: Varolline Demo Pvt Ltd\n"
            "Head office: Islamabad. Branch offices: Lahore and Karachi.\n"
            "Meeting note: Next quarter expansion planned in Faisalabad.\n"
            "Tax rate used for internal budgeting: 18% GST.\n"
            "Annual budget: 5,000,000 PKR.\n")

print("Data ready in data/")