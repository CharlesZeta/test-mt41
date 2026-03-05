//+------------------------------------------------------------------+
//|                                          mt4_ea_fixed_v2_9.mq4   |
//|   轮询拉取命令与执行 + 状态/持仓上报 + 暂停 + 面板 + 日志导出       |
//|   修复：空 commands[] 误解析为脏命令导致 side/symbol 为空           |
//|   新增：字段兼容性(symbol/instrument/pair, volume/lots/size)     |
//|   新增：交易权限自检 + 详细错误日志                                |
//+------------------------------------------------------------------+
#property strict
#property version   "2.9"

// ==================== 输入参数 ====================
input string base_url = "https://test-mt41-production.up.railway.app";
input int    poll_interval_sec = 1;
input int    status_interval_sec = 2;
input int    positions_interval_sec = 30;
input int    http_timeout_ms = 2000;
input bool   debug_echo_mode = false;
input string manual_account = ""; // 空字符串则自动获取当前账号

// ==================== 全局变量 ====================
string   g_account_str;
datetime g_last_poll_time = 0;
datetime g_last_status_time = 0;
datetime g_last_positions_time = 0;

int      g_last_http_code = 0;
string   g_last_error_msg = "";

int      g_poll_count = 0;
int      g_queue_batch_size = 0;
double   g_poll_latency_ms = 0;

int      g_reports_sent = 0;
int      g_executed_commands = 0;
int      g_failed_commands = 0;

int      g_position_pct = 50;  // 用户选择的仓位比例（默认50%）

string   g_fetch_info_line1 = "";
string   g_fetch_info_line2 = "";

bool     g_paused = false;

// 日志窗口
string g_log_lines[];
int    g_log_max_lines = 20;

// 幂等
string g_executed_cmd_ids[];

// 风险：允许时间漂移
#define MAX_TIME_DRIFT 60

// 重试
#define MAX_RETRY 3
#define RETRY_DELAY_MS 500

// ==================== 工具函数 ====================

string NormalizeUrl(string base, string path) {
   if (StringLen(base) > 0 && StringGetCharacter(base, StringLen(base)-1) == '/')
      base = StringSubstr(base, 0, StringLen(base)-1);
   if (StringLen(path) == 0 || StringGetCharacter(path, 0) != '/')
      path = "/" + path;
   return base + path;
}

string Trim(string s) {
   int start=0, end=StringLen(s)-1;
   while(start<=end && StringGetCharacter(s,start)<=32) start++;
   while(end>=start && StringGetCharacter(s,end)<=32) end--;
   if(end<start) return "";
   return StringSubstr(s,start,end-start+1);
}

string ToLower(string s) {
   string r="";
   for(int i=0;i<StringLen(s);i++){
      int ch=StringGetCharacter(s,i);
      if(ch>='A' && ch<='Z') ch+=32;
      r += ShortToString((ushort)ch);
   }
   return r;
}

string StringUpperCase(string s){
   string r="";
   for(int i=0;i<StringLen(s);i++){
      int ch=StringGetCharacter(s,i);
      if(ch>='a' && ch<='z') ch-=32;
      r += ShortToString((ushort)ch);
   }
   return r;
}

// side 归一化：BUY/SELL/long/short/b/s
string NormSide(string x){
   string s = ToLower(Trim(x));
   if(s=="buy" || s=="sell") return s;
   if(s=="b" || s=="long") return "buy";
   if(s=="s" || s=="short") return "sell";
   return "";
}

// 交易权限自检：集中输出排查信息
bool CheckTradeAllowed(){
   bool allowed = IsTradeAllowed();

   Print("=== TRADE PERMISSION CHECK ===");
   Print("IsTradeAllowed() = ", allowed);
   Print("Account: ", AccountNumber(), " / ", AccountName(), " / ", AccountServer());
   Print("如交易被拒绝，请检查：");
   Print("1) 顶部 AutoTrading 是否为绿色");
   Print("2) EA 属性是否勾选 Allow live trading");
   Print("3) Tools->Options->Expert Advisors 中是否禁止自动交易");
   Print("4) 是否使用投资者密码登录，或当前品种不在交易时段");

   if(!allowed){
      g_last_error_msg = "trade_not_allowed_by_terminal";
      return false;
   }
   return true;
}

// ==================== HTTP ====================
int httppostjson(const string url, const string json_body, string &response_body, string &response_headers)
{
   char post[];
   StringToCharArray(json_body, post, 0, WHOLE_ARRAY, CP_UTF8);
   int data_size = ArraySize(post) - 1;
   if(data_size < 0) data_size = 0;

   char result[];
   int res = WebRequest("POST", url, "", "", http_timeout_ms, post, data_size, result, response_headers);
   response_body = CharArrayToString(result);

   g_fetch_info_line1 = "POST " + url + " | res=" + IntegerToString(res) + " | size=" + IntegerToString(data_size);
   g_fetch_info_line2 = "lastErr=" + IntegerToString(GetLastError()) + " | resp=" + (StringLen(response_body)>200? StringSubstr(response_body,0,200)+"..." : response_body);

   Print("httppostjson url=",url," res=",res," lastError=",GetLastError(),
         " respBodyPreview=",(StringLen(response_body)>200? StringSubstr(response_body,0,200)+"..." : response_body));
   return res;
}

// ==================== JSON（修复版解析）====================
class JSONObject;

class JSONArray {
private:
   string m_items[];
   int    m_count;
public:
   JSONArray(){ m_count=0; ArrayResize(m_items,0); }

   void Parse(string json_str){
      ArrayResize(m_items,0);
      m_count=0;

      json_str = Trim(json_str);
      if(json_str=="") return;

      // 关键：状态机分割，避免空数组/字符串导致脏 item
      bool in_str=false;
      bool esc=false;
      int  depth_obj=0;
      int  depth_arr=0;
      string cur="";

      for(int i=0;i<StringLen(json_str);i++){
         ushort ch = StringGetCharacter(json_str,i);

         if(esc){
            cur += ShortToString(ch);
            esc=false;
            continue;
         }
         if(in_str){
            if(ch=='\\'){
               cur += ShortToString(ch);
               esc=true;
               continue;
            }
            if(ch=='"'){
               in_str=false;
            }
            cur += ShortToString(ch);
            continue;
         }

         if(ch=='"'){ in_str=true; cur += ShortToString(ch); continue; }
         if(ch=='{'){ depth_obj++; cur += ShortToString(ch); continue; }
         if(ch=='}'){ depth_obj--; cur += ShortToString(ch); continue; }
         if(ch=='['){ depth_arr++; cur += ShortToString(ch); continue; }
         if(ch==']'){ depth_arr--; cur += ShortToString(ch); continue; }

         // 只有在最外层（不在字符串，不在任何 {} / []）才按逗号分割
         if(ch==',' && depth_obj==0 && depth_arr==0){
            string t = Trim(cur);
            if(t!=""){
               ArrayResize(m_items,m_count+1);
               m_items[m_count]=t;
               m_count++;
            }
            cur="";
            continue;
         }
         cur += ShortToString(ch);
      }

      string tail = Trim(cur);
      if(tail!=""){
         ArrayResize(m_items,m_count+1);
         m_items[m_count]=tail;
         m_count++;
      }
   }

   int Size(){ return m_count; }

   bool GetObject(int index, JSONObject &obj);
   bool GetString(int index, string &value){
      if(index<0 || index>=m_count) return false;
      string item = Trim(m_items[index]);
      if(StringLen(item)>=2 && StringGetCharacter(item,0)=='"' && StringGetCharacter(item,StringLen(item)-1)=='"'){
         value = StringSubstr(item,1,StringLen(item)-2);
         return true;
      }
      return false;
   }

   string ToString(){
      string r="";
      for(int i=0;i<m_count;i++){
         if(i>0) r += ",";
         r += m_items[i];
      }
      return r;
   }

   void AddObject(JSONObject &obj);
};

class JSONObject {
private:
   string m_data; // 不含最外层 {}
public:
   JSONObject(){ m_data=""; }
   void SetData(string data){ m_data=data; }
   string GetData(){ return m_data; }

   bool IsEmpty(){
      string t = Trim(m_data);
      return (t=="" || t=="{}");
   }

   void SetString(string key, string value){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":\"" + value + "\"";
   }
   void SetInt(string key, int value){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":" + IntegerToString(value);
   }
   void SetDouble(string key, double value){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":" + DoubleToString(value,8);
   }
   void SetBool(string key, bool value){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":" + (value?"true":"false");
   }
   void SetObject(string key, JSONObject &obj){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":{" + obj.GetData() + "}";
   }
   void SetArray(string key, JSONArray &arr){
      if(m_data!="") m_data+=",";
      m_data += "\"" + key + "\":[" + arr.ToString() + "]";
   }

   bool GetString(string key, string &value){
      string pattern="\""+key+"\":\"";
      int pos=StringFind(m_data,pattern);
      if(pos<0) return false;
      pos += StringLen(pattern);
      int end=StringFind(m_data,"\"",pos);
      if(end<0) return false;
      value = StringSubstr(m_data,pos,end-pos);
      return true;
   }

   bool GetInt(string key, int &value){
      string pattern="\""+key+"\":";
      int pos=StringFind(m_data,pattern);
      if(pos<0) return false;
      pos += StringLen(pattern);
      int end=StringFind(m_data,",",pos);
      if(end<0) end=StringFind(m_data,"}",pos);
      if(end<0) return false;
      string v=Trim(StringSubstr(m_data,pos,end-pos));
      value=(int)StringToInteger(v);
      return true;
   }

   bool GetDouble(string key, double &value){
      string pattern="\""+key+"\":";
      int pos=StringFind(m_data,pattern);
      if(pos<0) return false;
      pos += StringLen(pattern);
      int end=StringFind(m_data,",",pos);
      if(end<0) end=StringFind(m_data,"}",pos);
      if(end<0) return false;
      string v=Trim(StringSubstr(m_data,pos,end-pos));
      value=StringToDouble(v);
      return true;
   }

   bool GetBool(string key, bool &value){
      string pattern="\""+key+"\":";
      int pos=StringFind(m_data,pattern);
      if(pos<0) return false;
      pos += StringLen(pattern);
      int end=StringFind(m_data,",",pos);
      if(end<0) end=StringFind(m_data,"}",pos);
      if(end<0) return false;
      string v=Trim(StringSubstr(m_data,pos,end-pos));
      if(v=="true"){ value=true; return true; }
      if(v=="false"){ value=false; return true; }
      return false;
   }

   bool GetArray(string key, JSONArray &arr){
      string pattern="\""+key+"\":[";
      int pos=StringFind(m_data,pattern);
      if(pos<0) return false;

      pos += StringLen(pattern);
      int depth=1;
      bool in_str=false, esc=false;
      int i=pos;

      while(i<StringLen(m_data) && depth>0){
         ushort ch = StringGetCharacter(m_data,i);

         if(esc){ esc=false; i++; continue; }
         if(in_str){
            if(ch=='\\'){ esc=true; i++; continue; }
            if(ch=='"'){ in_str=false; }
            i++; continue;
         }
         if(ch=='"'){ in_str=true; i++; continue; }
         if(ch=='[') depth++;
         if(ch==']') depth--;
         i++;
      }

      // 取 [ ... ] 内部
      string arr_str = "";
      if(i-pos-1>0) arr_str = StringSubstr(m_data,pos,i-pos-1);
      arr.Parse(arr_str);
      return true;
   }

   string ToString(){ return "{"+m_data+"}"; }
};

// 兼容字段读取：symbol/instrument/pair, volume/lots/size
string GetFieldStr(JSONObject &obj, string key1, string key2="", string key3=""){
   string val="";
   if(obj.GetString(key1,val) && Trim(val)!="") return Trim(val);
   if(key2!=""){
      if(obj.GetString(key2,val) && Trim(val)!="") return Trim(val);
   }
   if(key3!=""){
      if(obj.GetString(key3,val) && Trim(val)!="") return Trim(val);
   }
   return "";
}

double GetFieldDouble(JSONObject &obj, string key1, string key2="", string key3=""){
   double val=0;
   if(obj.GetDouble(key1,val) && val>0) return val;
   if(key2!=""){
      if(obj.GetDouble(key2,val) && val>0) return val;
   }
   if(key3!=""){
      if(obj.GetDouble(key3,val) && val>0) return val;
   }
   return 0;
}

int GetFieldInt(JSONObject &obj, string key1, string key2="", string key3=""){
   int val=0;
   if(obj.GetInt(key1,val) && val>0) return val;
   if(key2!=""){
      if(obj.GetInt(key2,val) && val>0) return val;
   }
   if(key3!=""){
      if(obj.GetInt(key3,val) && val>0) return val;
   }
   return 0;
}

bool JSONArray::GetObject(int index, JSONObject &obj){
   if(index<0 || index>=m_count) return false;
   obj.SetData(m_items[index]);
   return true;
}
void JSONArray::AddObject(JSONObject &obj){
   ArrayResize(m_items,m_count+1);
   m_items[m_count]=obj.ToString();
   m_count++;
}

// ParseJSON：把最外层 {} 去掉
bool ParseJSON(string json_str, JSONObject &obj){
   json_str = Trim(json_str);
   if(StringLen(json_str)>=2 && StringGetCharacter(json_str,0)=='{' && StringGetCharacter(json_str,StringLen(json_str)-1)=='}'){
      json_str = StringSubstr(json_str,1,StringLen(json_str)-2);
   }
   obj.SetData(json_str);
   return true;
}

// ==================== 日志 ====================
void AddLog(string action, string status, string msg){
   string time_str = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);
   string line = time_str+" | "+action+" | "+status+" | "+msg;
   for(int i=g_log_max_lines-1;i>0;i--) g_log_lines[i]=g_log_lines[i-1];
   g_log_lines[0]=line;
}

// ==================== 幂等 ====================
bool IsCommandExecuted(string cmd_id){
   int n=ArraySize(g_executed_cmd_ids);
   for(int i=0;i<n;i++) if(g_executed_cmd_ids[i]==cmd_id) return true;
   return false;
}
void MarkCommandExecuted(string cmd_id){
   int n=ArraySize(g_executed_cmd_ids);
   ArrayResize(g_executed_cmd_ids,n+1);
   g_executed_cmd_ids[n]=cmd_id;
   if(n>1000){
      ArrayCopy(g_executed_cmd_ids,g_executed_cmd_ids,0,n-1000,1000);
      ArrayResize(g_executed_cmd_ids,1000);
   }
}

// ==================== 面板（简版保留）====================
void CreateLabel(string name,int x,int y,string text,color clr,int font_size){
   ObjectCreate(0,name,OBJ_LABEL,0,0,0);
   ObjectSetInteger(0,name,OBJPROP_XDISTANCE,x);
   ObjectSetInteger(0,name,OBJPROP_YDISTANCE,y);
   ObjectSetString(0,name,OBJPROP_TEXT,text);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_FONTSIZE,font_size);
   ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER);
}
void CreateDashboard(){
   CreateLabel("dashboard_title",20,20,"mt4 ea 追踪面板(v2.9)",clrYellow,12);
   CreateLabel("data_poll",20,45,"",clrLime,9);
   CreateLabel("data_cmd",20,65,"",clrLime,9);
   CreateLabel("data_fetch1",20,85,"",clrSilver,8);
}
void UpdateDashboard(){
   string poll="poll(ms):"+DoubleToString(g_poll_latency_ms,0)+" http:"+IntegerToString(g_last_http_code)+" batch:"+IntegerToString(g_queue_batch_size);
   ObjectSetString(0,"data_poll",OBJPROP_TEXT,poll);

   string cmd="ok:"+IntegerToString(g_executed_commands)+" fail:"+IntegerToString(g_failed_commands)+" report:"+IntegerToString(g_reports_sent);
   ObjectSetString(0,"data_cmd",OBJPROP_TEXT,cmd);

   ObjectSetString(0,"data_fetch1",OBJPROP_TEXT,g_fetch_info_line1);
}
void DeleteDashboard(){
   ObjectDelete(0,"dashboard_title");
   ObjectDelete(0,"data_poll");
   ObjectDelete(0,"data_cmd");
   ObjectDelete(0,"data_fetch1");
}

// ==================== 上报 ====================
void SendReport(string cmd_id,string nonce,bool ok,int ticket,string error_code,string error_msg,int exec_ms=0){
   JSONObject rep;
   rep.SetString("account",g_account_str);
   rep.SetString("cmd_id",cmd_id);
   rep.SetString("nonce",nonce);
   rep.SetBool("ok",ok);
   rep.SetInt("ticket",ticket);
   rep.SetString("error",error_code);
   rep.SetString("message",error_msg);
   rep.SetInt("exec_ms",exec_ms);
   rep.SetInt("ts",(int)TimeCurrent());

   string url = NormalizeUrl(base_url,"/web/api/mt4/report");
   string resp, hdr;
   int res = httppostjson(url, rep.ToString(), resp, hdr);
   if(res!=-1) g_reports_sent++;
}

// 状态上报（保留你原先字段的核心子集）
void PostStatus(){
   JSONObject st;
   st.SetString("account",g_account_str);
   st.SetString("server",AccountServer());
   st.SetInt("ts",(int)TimeCurrent());
   st.SetDouble("balance",AccountBalance());
   st.SetDouble("equity",AccountEquity());
   st.SetDouble("margin",AccountMargin());
   st.SetDouble("free_margin",AccountFreeMargin());
   
   // 浮动盈亏
   st.SetDouble("floating_pnl", AccountProfit());
   
   // 杠杆使用情况 = 总保证金占用 / 账户权益
   double equity = AccountEquity();
   double margin = AccountMargin();
   int leverage_used = 0;
   if(equity > 0 && margin > 0){
      leverage_used = (int)(margin / equity * 100);
   }
   st.SetInt("leverage_used", leverage_used);
   
   // 风控标记（可以根据需要扩展）
   string risk_flags = "";
   double marginLevel = (equity > 0 && margin > 0) ? (equity / margin * 100) : 0;
   if(marginLevel > 0 && marginLevel < 150){
      risk_flags = "low_margin_level";
   }else if(AccountFreeMargin() < AccountBalance() * 0.1){
      risk_flags = "low_free_margin";
   }
   st.SetString("risk_flags", risk_flags);
   
   JSONObject metrics;
   metrics.SetDouble("poll_latency_ms",g_poll_latency_ms);
   metrics.SetInt("last_http_code",g_last_http_code);
   metrics.SetString("last_error",g_last_error_msg);
   metrics.SetInt("queue_batch_size",g_queue_batch_size);
   metrics.SetInt("reports_sent_count",g_reports_sent);
   metrics.SetInt("executed_commands",g_executed_commands);
   metrics.SetInt("failed_commands",g_failed_commands);
   metrics.SetInt("position_pct",g_position_pct);  // 用户选择的仓位比例
   
   // 同时上报到 metrics 里（兼容性）
   metrics.SetDouble("floating_pnl", AccountProfit());
   metrics.SetInt("leverage_used", leverage_used);
   metrics.SetString("risk_flags", risk_flags);
   
   st.SetObject("metrics",metrics);

   string url = NormalizeUrl(base_url,"/web/api/mt4/status");
   string resp,hdr;
   httppostjson(url,st.ToString(),resp,hdr);
}

// 持仓上报（增强版：含每点价值）
string OrderTypeToString(int type){
   if(type==OP_BUY) return "buy";
   if(type==OP_SELL) return "sell";
   if(type==OP_BUYLIMIT) return "buylimit";
   if(type==OP_SELLLIMIT) return "selllimit";
   if(type==OP_BUYSTOP) return "buystop";
   if(type==OP_SELLSTOP) return "sellstop";
   return "unknown";
}

// 计算单个品种的每点价值（每波动1点对账户的资金影响）
double CalcSymbolPointValue(string symbol, double lots){
   double tick_value = MarketInfo(symbol, MODE_TICKVALUE);
   double tick_size = MarketInfo(symbol, MODE_TICKSIZE);
   double point = MarketInfo(symbol, MODE_POINT);
   
   // 每点价值 = tick_value * (point / tick_size)
   double point_value = 0;
   if(tick_size > 0 && point > 0){
      point_value = tick_value * (point / tick_size);
   }
   
   // 返回：手数 × 每点价值 = 该持仓每波动1点对账户的盈亏
   return lots * point_value;
}

// 估算该持仓保证金占用（不同券商/品种可能略有差异）
double CalcMarginRequired(string symbol, int order_type, double lots){
   double mr = MarketInfo(symbol, MODE_MARGINREQUIRED);
   if(mr > 0){
      return mr * lots;
   }

   // 兜底：用合约大小、价格、杠杆做一个粗略估算（主要用于展示）
   double contract = MarketInfo(symbol, MODE_LOTSIZE);
   double price = MarketInfo(symbol, MODE_ASK);
   int lev = AccountLeverage();
   if(contract > 0 && price > 0 && lev > 0){
      return (contract * lots * price) / lev;
   }
   return 0.0;
}

void PostPositions(){
   JSONArray arr;
   double total_point_value = 0;
   
   for(int i=0;i<OrdersTotal();i++){
      if(OrderSelect(i,SELECT_BY_POS)){
         JSONObject p;
         string sym = OrderSymbol();
         double lots = OrderLots();
         double contract_size = MarketInfo(sym, MODE_LOTSIZE);
         double point = MarketInfo(sym, MODE_POINT);
         double tick_value = MarketInfo(sym, MODE_TICKVALUE);
         double tick_size = MarketInfo(sym, MODE_TICKSIZE);
         
         // 每点价值
         double point_value = 0;
         if(tick_size > 0 && point > 0){
            point_value = tick_value * (point / tick_size);
         }
         
         // 判断买卖方向
         string side = "buy";
         if(OrderType() == OP_SELL || OrderType() == OP_SELLLIMIT || OrderType() == OP_SELLSTOP){
            side = "sell";
         }
         
         p.SetInt("ticket",OrderTicket());
         p.SetString("symbol",sym);
         p.SetString("side",side);
         p.SetString("type",OrderTypeToString(OrderType()));
         p.SetDouble("lots",lots);
         p.SetDouble("open_price",OrderOpenPrice());
         p.SetDouble("current_price",MarketInfo(sym, MODE_BID));  // 当前价格
         p.SetDouble("sl",OrderStopLoss());
         p.SetDouble("tp",OrderTakeProfit());
         p.SetInt("open_time",(int)OrderOpenTime());
         p.SetDouble("profit",OrderProfit());
         p.SetDouble("margin",CalcMarginRequired(sym, OrderType(), lots));  // 估算保证金
         p.SetDouble("commission",OrderCommission());  // 手续费
         p.SetDouble("swap",OrderSwap());  // 隔夜费
         p.SetInt("magic",OrderMagicNumber());  // Magic number
         // 增强字段
         p.SetDouble("contract_size",contract_size);
         p.SetDouble("point",point);
         p.SetDouble("tick_value",tick_value);
         p.SetDouble("point_value",point_value);
         
         // 累计每点价值
         double pv = CalcSymbolPointValue(sym, lots);
         p.SetDouble("exposure_per_point", pv);
         total_point_value += pv;
         
         arr.AddObject(p);
      }
   }
   
   JSONObject data;
   data.SetString("account",g_account_str);
   data.SetArray("positions",arr);
   // exposure_notional = 所有持仓每波动1点对账户的总盈亏
   data.SetDouble("exposure_notional", total_point_value);
   data.SetInt("ts",(int)TimeCurrent());

   string url = NormalizeUrl(base_url,"/web/api/mt4/positions");
   string resp,hdr;
   httppostjson(url,data.ToString(),resp,hdr);
}

// ==================== 执行（只修 side/symbol/volume 校验）====================
bool ExecuteMarket(JSONObject &cmd, int &ticket, string &error_code, string &error_msg){
   // 使用兼容字段读取：symbol/instrument/pair, volume/lots/size
   string symbol = GetFieldStr(cmd, "symbol", "instrument", "pair");
   string side = GetFieldStr(cmd, "side", "direction");
   double volume = GetFieldDouble(cmd, "volume", "lots", "size");

   symbol = Trim(StringUpperCase(symbol));  // 大写
   side = NormSide(side);

   // 详细日志
   Print("=== ExecuteMarket DEBUG ===");
   Print("symbol=", symbol, " side=", side, " volume=", DoubleToString(volume,2));

   if(symbol=="" || side=="" || volume<=0){
      error_code="bad_command";
      error_msg="缺少必要字段: symbol=\""+symbol+"\" side=\""+side+"\" volume="+DoubleToString(volume,2);
      return false;
   }

   // 交易权限自检
   if(!CheckTradeAllowed()){
      error_code="trade_not_allowed";
      error_msg="交易权限被拒绝，请检查MT4设置";
      return false;
   }

   RefreshRates();
   double bid=MarketInfo(symbol,MODE_BID);
   double ask=MarketInfo(symbol,MODE_ASK);
   if(bid==0 || ask==0){
      error_code="invalid_market";
      error_msg="无法获取报价: "+symbol;
      return false;
   }

   int order_type = (side=="buy") ? OP_BUY : OP_SELL;
   double price   = (order_type==OP_BUY) ? ask : bid;

   int slippage=3;
   color arrow_color = (order_type==OP_BUY)?clrBlue:clrRed;

   int retry=0, last_err=0;
   while(retry<MAX_RETRY){
      ticket = OrderSend(symbol,order_type,volume,price,slippage,0,0,"mt4_ea",0,0,arrow_color);
      if(ticket>=0) return true;

      last_err=GetLastError();
      if(last_err==130 || last_err==138 || last_err==146){
         retry++;
         Sleep(RETRY_DELAY_MS);
         RefreshRates();
         price = (order_type==OP_BUY)?Ask:Bid;
      }else break;
   }

   error_code="ordersend_failed";
   error_msg="ordersend 失败: "+IntegerToString(last_err);
   return false;
}

bool ExecuteLimit(JSONObject &cmd, int &ticket, string &error_code, string &error_msg){
   string symbol = GetFieldStr(cmd, "symbol", "instrument", "pair");
   string side   = GetFieldStr(cmd, "side", "direction");
   double volume = GetFieldDouble(cmd, "volume", "lots", "size");
   double price  = GetFieldDouble(cmd, "price", "limit_price", "entry_price");
   double sl     = GetFieldDouble(cmd, "sl", "sl_price", "stop_loss");
   double tp     = GetFieldDouble(cmd, "tp", "tp_price", "take_profit");

   symbol = Trim(StringUpperCase(symbol));
   side   = NormSide(side);

   Print("=== ExecuteLimit DEBUG ===");
   Print("symbol=", symbol, " side=", side, " volume=", DoubleToString(volume,2), " price=", DoubleToString(price,Digits));

   if(symbol=="" || side=="" || volume<=0 || price<=0){
      error_code="bad_command";
      error_msg="缺少必要字段(limit): symbol=\""+symbol+"\" side=\""+side+"\" volume="+DoubleToString(volume,2)+" price="+DoubleToString(price,Digits);
      return false;
   }

   if(!CheckTradeAllowed()){
      error_code="trade_not_allowed";
      error_msg="交易权限被拒绝，请检查MT4设置";
      return false;
   }

   int order_type = (side=="buy") ? OP_BUYLIMIT : OP_SELLLIMIT;
   int slippage   = 3;
   color arrow_color = (side=="buy") ? clrBlue : clrRed;

   int retry=0, last_err=0;
   while(retry<MAX_RETRY){
      ticket = OrderSend(symbol, order_type, volume, price, slippage,
                         (sl>0 ? sl : 0), (tp>0 ? tp : 0),
                         "mt4_ea_limit", 0, 0, arrow_color);
      if(ticket>=0) return true;

      last_err = GetLastError();
      if(last_err==130 || last_err==138 || last_err==146){
         retry++;
         Sleep(RETRY_DELAY_MS);
      }else{
         break;
      }
   }

   error_code="ordersend_failed";
   error_msg="limit ordersend 失败: "+IntegerToString(last_err);
   return false;
}

bool ExecuteClose(JSONObject &cmd, int &ticket, string &error_code, string &error_msg){
   int req_ticket = GetFieldInt(cmd, "ticket", "order", "position_id");
   double volume  = GetFieldDouble(cmd, "lots", "volume", "size");

   if(req_ticket<=0){
      error_code="bad_command";
      error_msg="缺少必要字段(close): ticket";
      return false;
   }

   if(!OrderSelect(req_ticket, SELECT_BY_TICKET)){
      error_code="order_not_found";
      error_msg="找不到订单: "+IntegerToString(req_ticket);
      return false;
   }

   if(!CheckTradeAllowed()){
      error_code="trade_not_allowed";
      error_msg="交易权限被拒绝，请检查MT4设置";
      return false;
   }

   int type = OrderType();
   double cur_lots = OrderLots();
   if(cur_lots<=0){
      error_code="invalid_order";
      error_msg="订单手数无效";
      return false;
   }

   double close_lots = cur_lots;
   if(volume>0 && volume<cur_lots)
      close_lots = volume;

   RefreshRates();
   double price;
   if(type==OP_BUY || type==OP_BUYLIMIT || type==OP_BUYSTOP)
      price = Bid;
   else
      price = Ask;

   int slippage = 3;
   color arrow_color = (type==OP_BUY || type==OP_BUYLIMIT || type==OP_BUYSTOP) ? clrRed : clrBlue;

   int retry=0, last_err=0;
   while(retry<MAX_RETRY){
      bool ok = OrderClose(req_ticket, close_lots, price, slippage, arrow_color);
      if(ok){
         ticket = req_ticket;
         return true;
      }
      last_err = GetLastError();
      if(last_err==130 || last_err==138 || last_err==146){
         retry++;
         Sleep(RETRY_DELAY_MS);
         RefreshRates();
         if(type==OP_BUY || type==OP_BUYLIMIT || type==OP_BUYSTOP)
            price = Bid;
         else
            price = Ask;
      }else{
         break;
      }
   }

   error_code="orderclose_failed";
   error_msg="orderclose 失败: "+IntegerToString(last_err);
   return false;
}

bool ExecuteQuote(JSONObject &cmd, string cmd_id, string nonce){
   string symbol = GetFieldStr(cmd, "symbol", "instrument", "pair");
   symbol = Trim(StringUpperCase(symbol));
   if(symbol=="") symbol = Symbol();

   RefreshRates();
   double bid = MarketInfo(symbol, MODE_BID);
   double ask = MarketInfo(symbol, MODE_ASK);

   Print("QUOTE: ", symbol, " bid=", DoubleToString(bid,Digits), " ask=", DoubleToString(ask,Digits),
         " cmd_id=", cmd_id, " nonce=", nonce);

   return true;
}

// ==================== 命令处理 ====================
void ProcessCommand(JSONObject &cmd){
   string id="", action="", nonce="";
   int ttl=10, created_at=0;

   cmd.GetString("id",id);
   cmd.GetString("action",action);
   cmd.GetString("nonce",nonce);
   cmd.GetInt("ttl_sec",ttl);
   cmd.GetInt("created_at",created_at);

   id = Trim(id);
   action = ToLower(Trim(action));
   if(id=="") id = nonce; // 最后兜底
   if(action=="") action="market";
   
   Print("=== ProcessCommand DEBUG === id=", id, " action=", action, " nonce=", nonce);

   if(IsCommandExecuted(id)){
      Print("=== DEBUG: duplicated ===");
      SendReport(id,nonce,false,0,"duplicated","命令已执行过");
      return;
   }

   int now=(int)TimeCurrent();
   int time_diff = now - created_at;
   Print("=== TIME DEBUG === now=", now, " created_at=", created_at, " ttl=", ttl, " diff=", time_diff);
   
   // 宽松的过期检查：允许最多2小时的时间差（应对后端时钟问题）
   // 但拒绝明显错误的时间差（比如未来时间）
   int max_allowed_diff = 7200; // 2小时
   if(created_at > 0){
      if(time_diff < 0 && MathAbs(time_diff) > 3600){
         // 命令时间是未来超过1小时，拒绝
         Print("=== DEBUG: future command rejected ===");
         SendReport(id,nonce,false,0,"future_command","命令时间在未来");
         return;
      }
      if(time_diff > max_allowed_diff){
         // 命令过期超过2小时，拒绝
         Print("=== DEBUG: expired (>2h) ===");
         SendReport(id,nonce,false,0,"expired","命令已过期");
         return;
      }
   }

   uint t0=GetTickCount();
   bool ok=false;
   int ticket=0;
   string ec="", em="";

   Print("=== DEBUG: calling ExecuteMarket ===");
   if(action=="market")      ok = ExecuteMarket(cmd,ticket,ec,em);
   else if(action=="limit")  ok = ExecuteLimit(cmd,ticket,ec,em);
   else if(action=="close")  ok = ExecuteClose(cmd,ticket,ec,em);
   else if(action=="quote")  { ok = ExecuteQuote(cmd,id,nonce); ec=""; em=""; }
   else { ec="unknown_action"; em="未知 action: "+action; ok=false; }

   int exec_ms = (int)(GetTickCount()-t0);

   if(ok){ MarkCommandExecuted(id); g_executed_commands++; }
   else  { g_failed_commands++; }

   Print("=== DEBUG: result ok=", ok, " ticket=", ticket, " ec=", ec);
   SendReport(id,nonce,ok,ticket,ec,em,exec_ms);
}

// ==================== 轮询 ====================
void PollCommands(){
   uint start=GetTickCount();

   string url = NormalizeUrl(base_url,"/web/api/mt4/commands");
   JSONObject req;
   req.SetString("account",g_account_str);
   req.SetInt("max",50);

   string resp,hdr;
   int res = httppostjson(url, req.ToString(), resp, hdr);

   g_poll_latency_ms = (GetTickCount()-start);
   g_poll_count++;

   if(res==-1){
      g_last_http_code=0;
      g_last_error_msg="webrequest_failed:"+IntegerToString(GetLastError());
      return;
   }

   int http_code=0;
   if(StringLen(hdr)>=12){
      string code_str = StringSubstr(hdr,9,3);
      http_code = StringToInteger(code_str);
   }
   g_last_http_code=http_code;

   if(http_code!=200){
      g_last_error_msg="http "+IntegerToString(http_code);
      return;
   }

   JSONObject root;
   ParseJSON(resp,root);
   
   Print("=== POLL RESPONSE DEBUG === resp=", resp);

   bool paused=false;
   if(root.GetBool("paused",paused)) g_paused=paused;

   JSONArray cmds;
   if(!root.GetArray("commands",cmds)){
      g_queue_batch_size=0;
      Print("=== DEBUG: GetArray failed ===");
      return;
   }

   int n = cmds.Size();
   g_queue_batch_size=n;
   Print("=== DEBUG: got ", n, " commands ===");
   if(n<=0) return; // ✅ 空数组直接返回，不再制造 bad_command

   for(int i=0;i<n;i++){
      JSONObject one;
      if(!cmds.GetObject(i,one)) {
         Print("=== DEBUG: GetObject failed for i=", i, " ===");
         continue;
      }

      // ✅ 过滤脏 item：空白、非对象
      string raw = Trim(one.GetData());
      Print("=== DEBUG: cmd[", i, "] raw=", raw, " ===");
      if(raw=="" || raw=="{}"){
         Print("skip empty cmd object");
         continue;
      }
      // 如果不是以 { 开头，也跳过（防止被误切出来的残片）
      if(StringLen(raw)>0 && StringGetCharacter(raw,0)!='{'){
         Print("skip non-object cmd chunk: ", raw);
         continue;
      }
      ProcessCommand(one);
   }
}

// ==================== 生命周期 ====================
int OnInit(){
   g_account_str = (StringLen(manual_account)>0) ? manual_account : IntegerToString(AccountNumber());

   ArrayResize(g_log_lines,g_log_max_lines);
   for(int i=0;i<g_log_max_lines;i++) g_log_lines[i]="";
   ArrayResize(g_executed_cmd_ids,0);

   CreateDashboard();
   EventSetTimer(poll_interval_sec);

   PostStatus();

   Print("mt4 ea 初始化完成，账户:",g_account_str);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason){
   EventKillTimer();
   DeleteDashboard();
}

void OnTimer(){
   datetime now=TimeCurrent();

   if(now-g_last_poll_time >= poll_interval_sec){
      PollCommands();
      g_last_poll_time=now;
   }
   if(now-g_last_status_time >= status_interval_sec){
      PostStatus();
      g_last_status_time=now;
   }
   if(now-g_last_positions_time >= positions_interval_sec){
      PostPositions();
      g_last_positions_time=now;
   }
   UpdateDashboard();
}
//+------------------------------------------------------------------+
