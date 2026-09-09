using System;
using System.IO;
using System.Diagnostics;
using System.Drawing;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Collections.Generic;
using System.Web.Script.Serialization;

class MiraboxLauncher : Form {
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly Label status = new Label { AutoSize=false, Left=24, Top=70, Width=560, Height=70, Text="点击启动，自动运行控制页、N4 连接和 Micro 桥接。" };
    readonly Button start = new Button { Text="启动全部", Left=24, Top=155, Width=120, Height=40 };
    readonly Button stop = new Button { Text="停止全部", Left=155, Top=155, Width=120, Height=40 };
    Process server, relay; IntPtr job; string url; bool busy;
    NotifyIcon tray; ContextMenuStrip trayMenu;
    readonly System.Windows.Forms.Timer serviceTimer=new System.Windows.Forms.Timer {Interval=2000};
    bool exitRequested, trayNoticeShown; readonly bool trayEnabled;
    bool checkingDevice; string deviceSummary="等待设备";
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a,string n);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr j,IntPtr p);
    [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr j,uint c);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr j,int c,ref Limits l,int s);
    [StructLayout(LayoutKind.Sequential)] struct Basic { public long a,b; public uint flags; public UIntPtr min,max; public uint count; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct Io { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct Limits { public Basic basic; public Io io; public UIntPtr a,b,c,d; }
    static readonly Color Canvas=Color.FromArgb(13,20,30),Surface=Color.FromArgb(23,34,48),Ink=Color.FromArgb(236,242,248),Muted=Color.FromArgb(159,177,196),Mint=Color.FromArgb(106,230,196);
    Label Caption(string value,int x,int y,int width,int height,float size,Color color,bool bold=false) {
        var label=new Label {Text=value,Left=x,Top=y,Width=width,Height=height,ForeColor=color,BackColor=Color.Transparent,Font=new Font("Microsoft YaHei UI",size,bold?FontStyle.Bold:FontStyle.Regular)};return label;
    }
    void StyleButton(Button button,bool primary) {
        button.FlatStyle=FlatStyle.Flat;button.FlatAppearance.BorderSize=primary?0:1;
        button.FlatAppearance.BorderColor=Color.FromArgb(59,79,100);
        button.BackColor=primary?Mint:Surface;button.ForeColor=primary?Canvas:Ink;
        button.Font=new Font("Microsoft YaHei UI",11,FontStyle.Bold);button.Cursor=Cursors.Hand;
        button.FlatAppearance.MouseOverBackColor=primary?Color.FromArgb(140,244,216):Color.FromArgb(36,54,72);
        button.UseVisualStyleBackColor=false;
    }
    MiraboxLauncher(bool enableTray=true) {
        trayEnabled=enableTray;
        Text="N4 Bridge · A1aZ";ClientSize=new Size(720,480);Font=new Font("Microsoft YaHei UI",10);FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;
        BackColor=Canvas;ForeColor=Ink;StartPosition=FormStartPosition.CenterScreen;AutoScaleMode=AutoScaleMode.Dpi;DoubleBuffered=true;
        try {Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);}catch{}
        var iconPath=Path.Combine(root,"assets/app/icon-master.png");
        if(File.Exists(iconPath)){var picture=new PictureBox {Left=30,Top=25,Width=84,Height=84,SizeMode=PictureBoxSizeMode.Zoom,Image=new Bitmap(iconPath)};Controls.Add(picture);}
        Controls.Add(Caption("N4 Bridge",130,25,400,45,25,Ink,true));
        Controls.Add(Caption("你的设备，连接你的工作流",132,77,440,28,11,Muted));
        Controls.Add(Caption("LOCAL  /  本机运行",508,33,190,30,10,Mint));
        var card=new Panel {Left=32,Top=130,Width=656,Height=174,BackColor=Surface};Controls.Add(card);
        card.Controls.Add(Caption("设备与服务",22,18,580,28,12,Ink,true));
        status.SetBounds(22,58,612,100);status.ForeColor=Muted;status.BackColor=Surface;status.Font=new Font("Microsoft YaHei UI",11);
        status.Text="准备就绪。点击启动，自动连接 N4 并运行 Micro 桥接。\n首次使用须先安装虚拟驱动。";card.Controls.Add(status);
        start.SetBounds(32,324,204,50);start.Text="启动全部";StyleButton(start,true);Controls.Add(start);
        stop.SetBounds(248,324,132,50);StyleButton(stop,false);Controls.Add(stop);
        var web=new Button {Text="打开控制页",Left=392,Top=324,Width=148,Height=50};StyleButton(web,false);Controls.Add(web);
        var logs=new Button {Text="查看日志",Left=552,Top=324,Width=136,Height=50};StyleButton(logs,false);Controls.Add(logs);
        Controls.Add(Caption("× 隐藏到系统托盘；— 最小化到任务栏。结束运行请在托盘选择“退出”。",32,395,656,38,10,Muted));
        Controls.Add(Caption("© 2026 A1aZ  ·  AGPL-3.0-only  ·  0.1.0-rc.3",32,439,530,22,9,Muted));
        Controls.Add(Caption("非官方兼容工具",560,439,140,22,9,Muted));
        start.Click+=async (s,e)=>await StartAll(); stop.Click+=(s,e)=>StopAll();
        web.Click+=(s,e)=>OpenControlPage();
        logs.Click+=(s,e)=>{Directory.CreateDirectory(Path.Combine(root,"logs")); Process.Start("explorer.exe", "\""+Path.Combine(root,"logs")+"\"");};
        if(trayEnabled)EnsureTray();
        serviceTimer.Tick+=async (s,e)=>{if(!busy && server!=null && (server.HasExited || (relay!=null && relay.HasExited))) {StopAll(); status.Text="服务已退出。请查看 logs 中的日志，再点击启动。若旧 Relay 仍运行，请先关闭旧命令窗口。";if(tray!=null&&!Visible)tray.ShowBalloonTip(4000,"N4 Bridge","服务已停止，双击托盘图标查看原因。",ToolTipIcon.Warning);}await CheckDeviceStatus();if(tray!=null)tray.Text=busy?"N4 Bridge · 正在处理":server!=null?"N4 Bridge · "+deviceSummary:"N4 Bridge · 服务已停止";};serviceTimer.Start();
    }
    static bool Flag(Dictionary<string,object> obj,string key){object v;return obj!=null&&obj.TryGetValue(key,out v)&&v is bool&&(bool)v;}
    static string DeviceSummary(Dictionary<string,object> data){
        object v;var state=data.TryGetValue("status",out v)?Convert.ToString(v):"unknown";
        var n4=data.TryGetValue("n4",out v)?v as Dictionary<string,object>:null;
        if(Flag(data,"stale"))return "设备状态已失联";
        if(state=="error"||state=="stopped")return "N4 未连接";
        if(state=="running"&&Flag(n4,"opened")&&Flag(n4,"ready"))return "N4 已连接";
        return "等待 N4 就绪";
    }
    async Task CheckDeviceStatus(){
        if(busy||checkingDevice||server==null||exitRequested)return;
        var owner=server;checkingDevice=true;
        try{var raw=await Task.Run(()=>Request("/api/native/status"));
            if(IsDisposed||busy||exitRequested||server!=owner)return;
            var data=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(raw);
            deviceSummary=DeviceSummary(data);object error;
            status.Text=deviceSummary+"。"+(data.TryGetValue("lastError",out error)&&error!=null?Convert.ToString(error):"可通过控制页查看详细状态。")+"\n"+url;
        }catch{if(!IsDisposed&&!busy&&!exitRequested&&server==owner){deviceSummary="设备状态不可用";status.Text="无法读取 N4 状态，请打开控制页检查。";}}
        finally{checkingDevice=false;}
    }
    void OpenControlPage(){if(url!=null&&server!=null&&!server.HasExited)Process.Start(url+"/real-n4");else {RestoreWindow();status.Text="服务尚未启动，请先点击“启动全部”。";}}
    void RestoreWindow(){ShowInTaskbar=true;Show();WindowState=FormWindowState.Normal;Activate();}
    void EnsureTray(){
        if(tray!=null)return;
        trayMenu=new ContextMenuStrip();
        trayMenu.Items.Add("打开主窗口",null,(s,e)=>RestoreWindow());
        trayMenu.Items.Add("打开控制页",null,(s,e)=>OpenControlPage());
        var stopItem=trayMenu.Items.Add("停止服务",null,(s,e)=>{if(!busy)StopAll();});
        trayMenu.Items.Add(new ToolStripSeparator());
        trayMenu.Items.Add("退出 N4 Bridge",null,(s,e)=>RequestExit());
        trayMenu.Opening+=(s,e)=>stopItem.Enabled=!busy&&server!=null;
        tray=new NotifyIcon {Icon=Icon??SystemIcons.Application,Text="N4 Bridge · 服务已停止",ContextMenuStrip=trayMenu,Visible=true};
        tray.DoubleClick+=(s,e)=>RestoreWindow();
    }
    void RequestExit(){exitRequested=true;if(busy){RestoreWindow();status.Text="正在结束当前启动步骤，随后退出并清理服务…";return;}Close();}
    protected override void OnFormClosing(FormClosingEventArgs e){
        // Windows logoff/shutdown must never be intercepted as hide-to-tray.
        if(trayEnabled&&!exitRequested&&e.CloseReason==CloseReason.UserClosing){
            EnsureTray();e.Cancel=true;Hide();ShowInTaskbar=false;
            if(!trayNoticeShown){trayNoticeShown=true;tray.ShowBalloonTip(3500,"N4 Bridge 已隐藏","后台服务不会因关闭窗口而停止。双击图标打开，右键选择退出。",ToolTipIcon.Info);}
        }else{exitRequested=true;StopAll();if(tray!=null)tray.Visible=false;}
        base.OnFormClosing(e);
    }
    protected override void Dispose(bool disposing){if(disposing){serviceTimer.Dispose();StopAll();if(tray!=null){tray.Visible=false;tray.Dispose();tray=null;}if(trayMenu!=null){trayMenu.Dispose();trayMenu=null;}}base.Dispose(disposing);}
    void CheckExit(){if(exitRequested)throw new OperationCanceledException("已取消启动。");}
    Process Launch(string exe,string args,string name) {
        var info=new ProcessStartInfo(Path.Combine(root,exe),args) {WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        info.EnvironmentVariables["MIRABOX_PYTHON"]=Path.Combine(root,"runtime/python/python.exe");
        info.EnvironmentVariables["MIRABOX_WEBUI_PORT"]=new Uri(url).Port.ToString();
        info.EnvironmentVariables["PYTHONUTF8"]="1"; info.EnvironmentVariables.Remove("PYTHONHOME"); info.EnvironmentVariables.Remove("PYTHONPATH");
        var p=new Process {StartInfo=info}; var gate=new object();
        DataReceivedEventHandler log=(s,e)=>{if(e.Data!=null) lock(gate) {try {File.AppendAllText(Path.Combine(root,"logs",name+".log"),DateTime.Now.ToString("s")+" "+e.Data+Environment.NewLine);} catch {}}};
        p.OutputDataReceived+=log; p.ErrorDataReceived+=log; p.Start();
        if(!AssignProcessToJobObject(job,p.Handle)) {p.Kill();throw new Exception("无法建立进程清理保护，已取消启动。");}
        p.BeginOutputReadLine(); p.BeginErrorReadLine(); return p;
    }
    static int RequestTimeout(string path){return path=="/api/native/start"?15000:5000;}
    string Request(string path,bool post=false) {
        var r=(HttpWebRequest)WebRequest.Create(url+path); r.Proxy=null;r.Timeout=RequestTimeout(path);r.ReadWriteTimeout=RequestTimeout(path);
        if(post) {r.Method="POST";r.ContentType="application/json";var data=System.Text.Encoding.UTF8.GetBytes("{}");r.ContentLength=data.Length;using(var w=r.GetRequestStream())w.Write(data,0,data.Length);}
        using(var response=r.GetResponse()) using(var reader=new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }
    async Task StartAll() {
        if(busy || server!=null)return;busy=true;start.Enabled=false;stop.Enabled=false;
        string step="启动控制页";
        try {
            // Never take over an existing listener or terminate somebody else's service.
            int port=18792; var probe=new TcpListener(IPAddress.Loopback,port);probe.Start();probe.Stop(); url="http://127.0.0.1:"+port;
            Directory.CreateDirectory(Path.Combine(root,"logs")); job=CreateJobObject(IntPtr.Zero,null);
            var limits=new Limits();limits.basic.flags=0x2000;
            if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits)))throw new Exception("无法初始化进程管理。");
            status.Text="正在启动控制页…"; server=Launch("runtime/node.exe","\""+Path.Combine(root,"webui/server.cjs")+"\"","webui");
            bool ready=false;
            for(int i=0;i<35;i++) {if(server.HasExited)throw new Exception("控制页启动失败，请查看日志。");try {var state=await Task.Run(()=>Request("/api/state"));if(state.Contains("mirabox.codex.micro.webui") && state.Contains("\"pid\":"+server.Id)) {ready=true;break;}}catch {} await Task.Delay(200);}
            CheckExit();if(!ready)throw new Exception("控制页未能就绪。");
            status.Text="正在启动 N4 与 Micro 桥接…";
            step="检查设备占用并启动 N4";
            await Task.Run(()=>Request("/api/native/start",true));
            CheckExit();
            step="启动 Micro Relay";
            relay=Launch("runtime/python/python.exe","scripts/micro-webui-relay.py --live --webui-url "+url,"relay");
            await Task.Delay(1200);CheckExit();if(relay.HasExited)throw new Exception("Micro 桥接未能启动：可能旧 Relay 正在运行，或虚拟驱动未安装。详见 relay.log。");
            status.Text="服务已启动。需要配置或查看设备状态时，点击“打开控制页”。\n"+url;
        } catch(Exception e) {StopAll();if(!IsDisposed)status.Text=step+"失败："+e.Message;} finally {busy=false;if(!IsDisposed){start.Enabled=true;stop.Enabled=true;if(exitRequested)Close();}}
    }
    void StopAll() {if(job!=IntPtr.Zero){TerminateJobObject(job,0);CloseHandle(job);job=IntPtr.Zero;} if(server!=null)server.Dispose();if(relay!=null)relay.Dispose();server=null;relay=null;status.Text="已停止本启动器的服务。可以重新启动。";}
    void SelfTest() {
        try {
            if(RequestTimeout("/api/native/start")<10000||RequestTimeout("/api/native/status")<5000)throw new Exception("HTTP timeout too short for backend process probes");
            var parser=new JavaScriptSerializer();
            if(DeviceSummary(parser.Deserialize<Dictionary<string,object>>("{\"status\":\"error\",\"n4\":{\"opened\":true,\"ready\":true}}"))!="N4 未连接")throw new Exception("Failed N4 incorrectly marked connected");
            if(DeviceSummary(parser.Deserialize<Dictionary<string,object>>("{\"status\":\"running\",\"stale\":true,\"n4\":{\"opened\":true,\"ready\":true}}"))!="设备状态已失联")throw new Exception("Stale N4 incorrectly marked connected");
            if(DeviceSummary(parser.Deserialize<Dictionary<string,object>>("{\"status\":\"running\",\"n4\":{\"opened\":true,\"ready\":true}}"))!="N4 已连接")throw new Exception("Ready N4 classification failed");
            Directory.CreateDirectory(Path.Combine(root,"logs"));
            var probe=new TcpListener(IPAddress.Loopback,0);probe.Start();int port=((IPEndPoint)probe.LocalEndpoint).Port;probe.Stop();url="http://127.0.0.1:"+port;
            job=CreateJobObject(IntPtr.Zero,null);var limits=new Limits();limits.basic.flags=0x2000;
            if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits)))throw new Exception("Job setup failed");
            server=Launch("runtime/node.exe","webui/server.cjs","selftest-webui");
            bool ready=false;for(int i=0;i<40;i++){try {ready=Request("/api/state").Contains("mirabox.codex.micro.webui");if(ready)break;}catch{}Thread.Sleep(150);}
            if(!ready)throw new Exception("WebUI unavailable");
            relay=Launch("runtime/python/python.exe","scripts/micro-webui-relay.py --webui-url "+url,"selftest-relay");
            if(!relay.WaitForExit(15000))throw new Exception("Python check timed out");
            relay.WaitForExit();if(relay.ExitCode!=0)throw new Exception("Python check failed");
            int pid=server.Id;
            if(trayEnabled){
                var hide=new FormClosingEventArgs(CloseReason.UserClosing,false);OnFormClosing(hide);
                if(!hide.Cancel||Visible||ShowInTaskbar||server.HasExited)throw new Exception("Tray hide stopped service or left window visible");
                RestoreWindow();Application.DoEvents();
                if(!Visible||!ShowInTaskbar||WindowState!=FormWindowState.Normal)throw new Exception("Tray restore failed");
                WindowState=FormWindowState.Minimized;if(!ShowInTaskbar)throw new Exception("Minimize removed taskbar entry");
                var shutdown=new FormClosingEventArgs(CloseReason.WindowsShutDown,false);OnFormClosing(shutdown);
                if(shutdown.Cancel||job!=IntPtr.Zero||tray.Visible)throw new Exception("Shutdown cleanup failed");
            }
            StopAll();Thread.Sleep(300);
            try {using(var p=Process.GetProcessById(pid)){if(!p.HasExited)throw new Exception("Child remained alive");}}catch(ArgumentException){}
            File.WriteAllText(Path.Combine(root,"logs/selftest-result.txt"),"PASS: bundled Node, Python, HTTP contract, check-only relay, process cleanup. No hardware opened."+(trayEnabled?" Tray hide/restore, taskbar minimize and shutdown cleanup passed.":""));
        }catch(Exception e){Environment.ExitCode=1;File.WriteAllText(Path.Combine(root,"logs/selftest-result.txt"),e.ToString());}finally{StopAll();}
    }
    [STAThread] static void Main(string[] args) {
        if(Array.IndexOf(args,"--preview")>=0){Application.EnableVisualStyles();using(var app=new MiraboxLauncher(false)){app.Show();Application.DoEvents();using(var bitmap=new Bitmap(app.Width,app.Height)){app.DrawToBitmap(bitmap,new Rectangle(0,0,app.Width,app.Height));bitmap.Save(Path.Combine(app.root,"launcher-preview.png"));}}return;}
        if(Array.IndexOf(args,"--self-test")>=0){using(var app=new MiraboxLauncher(false))app.SelfTest();return;}
        if(Array.IndexOf(args,"--tray-self-test")>=0){Application.EnableVisualStyles();using(var app=new MiraboxLauncher())app.SelfTest();return;}
        bool first;using(var mutex=new Mutex(true,"Local\\MiraboxPortableLauncher",out first)) {
            if(!first){MessageBox.Show("N4 Bridge 已经运行。请双击右下角系统托盘图标（可能在 ^ 隐藏图标内）打开窗口。");return;}
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new MiraboxLauncher());
        }
    }
}
