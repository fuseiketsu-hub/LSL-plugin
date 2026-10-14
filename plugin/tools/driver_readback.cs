using System;
using System.Reflection;

// 驱动回读工具：读取 Raw Accel 1.7 驱动当前活动配置并输出 JSON。
// 依赖 wrapper.dll 与 Newtonsoft.Json.dll（由调用方复制到本 exe 同目录）。
// 重新编译（仅需 Windows 自带 .NET Framework csc）：
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /out:driver_readback.exe driver_readback.cs
class DriverReadback
{
    static int Main()
    {
        try
        {
            var asm = Assembly.Load("wrapper");
            Type dc = null;
            foreach (var t in asm.GetTypes())
                if (t.Name == "DriverConfig") dc = t;
            if (dc == null) return 2;
            var ga = dc.GetMethod("GetActive",
                BindingFlags.Public | BindingFlags.Static | BindingFlags.Instance | BindingFlags.DeclaredOnly);
            object r = ga.Invoke(ga.IsStatic ? null : Activator.CreateInstance(dc), null);
            var tj = r.GetType().GetMethod("ToJSON",
                BindingFlags.Public | BindingFlags.Instance | BindingFlags.FlattenHierarchy);
            Console.WriteLine(tj.Invoke(r, null));
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine(e.GetType().FullName + ": " + e.Message);
            return 1;
        }
    }
}
