// Windowless launcher for bridge.py.
// python.exe is a console program: starting it from wscript/cmd pops a Windows
// Terminal window (which steals focus). This tiny winexe launches it with
// CREATE_NO_WINDOW so no console window is ever created.
//   launch.exe <workdir> <exe> [args...]
using System;
using System.Diagnostics;
using System.Text;

class Launch
{
    static int Main(string[] args)
    {
        if (args.Length < 2) return 2;
        try
        {
            StringBuilder sb = new StringBuilder();
            for (int i = 2; i < args.Length; i++)
            {
                if (i > 2) sb.Append(' ');
                sb.Append('"');
                sb.Append(args[i].Replace("\"", "\\\""));
                sb.Append('"');
            }
            ProcessStartInfo psi = new ProcessStartInfo(args[1], sb.ToString());
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WorkingDirectory = args[0];
            Process p = Process.Start(psi);
            return p == null ? 3 : 0;
        }
        catch (Exception)
        {
            return 1;
        }
    }
}
