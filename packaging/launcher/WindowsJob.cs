using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

internal sealed class WindowsJob : IDisposable
{
    private readonly object gate = new object();
    private IntPtr job;

    internal WindowsJob()
    {
        job = Native.CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        var limits = new Native.JobLimits();
        limits.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (!Native.SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits)))
        {
            var error = new Win32Exception();
            Dispose();
            throw error;
        }
    }

    internal OwnedProcess Start(string executable, string[] arguments, string root, string log)
    {
        lock (gate)
        {
            if (job == IntPtr.Zero) throw new ObjectDisposedException("WindowsJob");
            var security = new Native.SecurityAttributes();
            security.Length = Marshal.SizeOf(security);
            security.Inherit = true;
            // FILE_APPEND_DATA and GENERIC_READ, with inheritable handles for stdin/out/err.
            using (var output = Native.CreateFile(log, 4, 3, ref security, 4, 128, IntPtr.Zero))
            using (var input = Native.CreateFile("NUL", 0x80000000, 3, ref security, 3, 128, IntPtr.Zero))
            {
                if (output.IsInvalid || input.IsInvalid) throw new Win32Exception();
                var startup = new Native.StartupInfo();
                startup.Size = Marshal.SizeOf(startup);
                startup.Flags = 0x100; // STARTF_USESTDHANDLES
                startup.Input = input.DangerousGetHandle();
                startup.Output = startup.Error = output.DangerousGetHandle();
                var command = new StringBuilder(Quote(executable));
                foreach (string argument in arguments) command.Append(' ').Append(Quote(argument));
                Native.ProcessInfo process;
                const uint createSuspended = 4, createNoWindow = 0x08000000;
                if (!Native.CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                    createSuspended | createNoWindow, IntPtr.Zero, root, ref startup, out process))
                    throw new Win32Exception();
                try
                {
                    if (!Native.AssignProcessToJobObject(job, process.Process) ||
                        Native.ResumeThread(process.Thread) == uint.MaxValue)
                        throw new Win32Exception();
                    return new OwnedProcess(process.Process);
                }
                catch
                {
                    Native.TerminateProcess(process.Process, 1);
                    Native.CloseHandle(process.Process);
                    throw;
                }
                finally { Native.CloseHandle(process.Thread); }
            }
        }
    }

    internal void RequestClose()
    {
        Native.EnumWindows(delegate(IntPtr window, IntPtr unused)
        {
            if (!Native.IsWindowVisible(window)) return true;
            uint owner;
            Native.GetWindowThreadProcessId(window, out owner);
            // Query membership directly, since pythonw.exe can start a venv redirector child.
            IntPtr process = Native.OpenProcess(0x1000, false, owner); // PROCESS_QUERY_LIMITED_INFORMATION
            if (process == IntPtr.Zero) return true;
            try
            {
                bool member;
                if (Native.IsProcessInJob(process, job, out member) && member)
                    Native.PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero); // WM_CLOSE
            }
            finally { Native.CloseHandle(process); }
            return true;
        }, IntPtr.Zero);
    }

    public void Dispose()
    {
        lock (gate)
        {
            if (job == IntPtr.Zero) return;
            Native.CloseHandle(job);
            job = IntPtr.Zero;
        }
    }

    private static string Quote(string argument)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in argument)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') result.Append('\\', slashes + 1);
            result.Append('\\', slashes).Append(character);
            slashes = 0;
        }
        return result.Append('\\', 2 * slashes).Append('"').ToString();
    }
}

internal sealed class OwnedProcess : IDisposable
{
    private readonly IntPtr handle;
    internal OwnedProcess(IntPtr handle) { this.handle = handle; }

    internal bool Wait(uint milliseconds)
    {
        uint result = Native.WaitForSingleObject(handle, milliseconds);
        if (result == uint.MaxValue) throw new Win32Exception();
        return result == 0;
    }

    internal int ExitCode()
    {
        uint code;
        if (!Native.GetExitCodeProcess(handle, out code)) throw new Win32Exception();
        return unchecked((int)code);
    }

    public void Dispose() { Native.CloseHandle(handle); }
}

internal static class Native
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct SecurityAttributes
    {
        internal int Length;
        internal IntPtr Descriptor;
        [MarshalAs(UnmanagedType.Bool)] internal bool Inherit;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct BasicJobLimits
    {
        internal long ProcessTime, JobTime;
        internal uint LimitFlags;
        internal UIntPtr MinWorkingSet, MaxWorkingSet;
        internal uint ActiveProcessLimit;
        internal UIntPtr Affinity;
        internal uint Priority, Scheduling;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobLimits
    {
        internal BasicJobLimits Basic;
        internal ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
        internal UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct StartupInfo
    {
        internal int Size;
        internal IntPtr Reserved, Desktop, Title;
        internal uint X, Y, Width, Height, XChars, YChars, Fill, Flags;
        internal ushort ShowWindow, ReservedSize;
        internal IntPtr ReservedData, Input, Output, Error;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct ProcessInfo
    {
        internal IntPtr Process, Thread;
        internal uint ProcessId, ThreadId;
    }

    internal delegate bool WindowCallback(IntPtr window, IntPtr argument);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool SetInformationJobObject(IntPtr job, int info, ref JobLimits limits, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool member);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern SafeFileHandle CreateFile(string name, uint access, uint share, ref SecurityAttributes security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern bool CreateProcess(string executable, StringBuilder command, IntPtr processSecurity,
        IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string root, ref StartupInfo startup,
        out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern IntPtr OpenProcess(uint access, bool inherit, uint process);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("user32.dll")]
    internal static extern bool EnumWindows(WindowCallback callback, IntPtr argument);
    [DllImport("user32.dll")]
    internal static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")]
    internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")]
    internal static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
}
