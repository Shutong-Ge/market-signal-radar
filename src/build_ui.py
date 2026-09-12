# -*- coding: utf-8 -*-
"""装配单文件工作台：把 output/ui_data.json 注入模板"""
import os
B=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tpl=open(os.path.join(B,"src","ui_template.html"),encoding="utf-8").read()
data=open(os.path.join(B,"output","ui_data.json"),encoding="utf-8").read()
dst=os.path.join(B,"投研舆情雷达_热点监控工作台.html")
open(dst,"w",encoding="utf-8").write(tpl.replace("const D=__DATA__;","const D="+data+";"))
print(f"built {os.path.basename(dst)} {os.path.getsize(dst)/1024:.0f}KB")
