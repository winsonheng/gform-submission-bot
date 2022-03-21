import sys
from pathlib import Path
from gformhelper import GFormHelper
import re

gf = GFormHelper("https://docs.google.com/forms/d/e/1FAIpQLSfehp0liqFfCV9NgWj_sNj-DNvx0enJQS_Tvy2MVr98j_ISNQ/viewform")

root_path = Path(__file__).parent.parent

questions = {}
pages = {}
with open(root_path / 'questionids.txt','r') as inf:
    for line in inf:
        questions = eval(line)

print(questions)

for key, value in questions.items():
    pages[key] = gf.get_page_number(value)

print(pages)

numbers = list(range(1, 50))
print(numbers)
for i in numbers:
    if i < 20:
        numbers.remove(i)

print(numbers)

REMINDER_JOB_NAME_REGEX = re.compile("[0-9]{2}:[0-9]{2}")
if REMINDER_JOB_NAME_REGEX.match("11:052  -37862834483dffr"):
    print("YES")

#sys.exit()

for qn in gf.form_data:
    #print(str(len(qn)) + " : " +str(qn))
    if(len(qn)>4):
        print(qn[1] + " : " + str(qn[4]))

