using System;
using System.Drawing;
using System.Runtime.InteropServices;

namespace System.Windows.Forms
{
    public struct Message
    {
        public IntPtr HWnd { get; set; }
        public int Msg { get; set; }
        public IntPtr WParam { get; set; }
        public IntPtr LParam { get; set; }
        public IntPtr Result { get; set; }
        public object GetLParam(Type cls) => Marshal.PtrToStructure(LParam, cls);
    }
    public enum DialogResult { None = 0, OK = 1, Cancel = 2, Abort = 3, Retry = 4, Ignore = 5, Yes = 6, No = 7 }
    public enum MessageBoxButtons { OK = 0, OKCancel = 1, AbortRetryIgnore = 2, YesNoCancel = 3, YesNo = 4, RetryCancel = 5 }
    public enum AutoScaleMode { None = 0, Font = 1, Dpi = 2, Inherit = 3 }
    public enum FormWindowState { Normal = 0, Minimized = 1, Maximized = 2 }

    public static class MessageBox
    {
        static DialogResult Report(string text, string caption)
        {
            Console.Error.WriteLine("[wrrusb2a message] " + (string.IsNullOrEmpty(caption) ? "" : caption + ": ") + text);
            Console.Error.Flush();
            return DialogResult.OK;
        }
        public static DialogResult Show(string text) => Report(text, null);
        public static DialogResult Show(string text, string caption) => Report(text, caption);
        public static DialogResult Show(string text, string caption, MessageBoxButtons buttons) => Report(text, caption);
    }

    public class Control : System.ComponentModel.Component
    {
        static int nextHandle = 0x1000;
        readonly IntPtr handle = new IntPtr(nextHandle++);
        public IntPtr Handle => handle;
        public string Name { get; set; } = "";
        public virtual string Text { get; set; } = "";
        public virtual bool AutoSize { get; set; }
        public Point Location { get; set; }
        public Size Size { get; set; }
        public int TabIndex { get; set; }
        public bool Visible { get; set; }
        public bool Enabled { get; set; } = true;
        public ControlCollection Controls { get; } = new ControlCollection();
        public void SuspendLayout() { }
        public void ResumeLayout() { }
        public void ResumeLayout(bool performLayout) { }
        public void PerformLayout() { }
        public void BringToFront() { }
        public void Invalidate() { }
        public void Refresh() { }
        public void Update() { }
        public void Show() { Visible = true; }
        public void Hide() { Visible = false; }
        protected virtual void WndProc(ref Message m) { }
        protected virtual void OnLoad(EventArgs e) { }
        protected override void Dispose(bool disposing) { base.Dispose(disposing); }
        public class ControlCollection
        {
            public void Add(Control value) { }
            public void AddRange(Control[] controls) { }
            public int Count => 0;
        }
    }
    public class ContainerControl : Control
    {
        public SizeF AutoScaleDimensions { get; set; }
        public AutoScaleMode AutoScaleMode { get; set; }
    }
    public class Form : ContainerControl
    {
        public Size ClientSize { get; set; }
        public bool ShowInTaskbar { get; set; }
        public FormWindowState WindowState { get; set; }
        public void Close() { Visible = false; }
        public DialogResult ShowDialog() => DialogResult.OK;
        protected override void WndProc(ref Message m) { base.WndProc(ref m); }
        protected override void Dispose(bool disposing) { base.Dispose(disposing); }
    }
    public class Label : Control { }
    public class ProgressBar : Control
    {
        public int Minimum { get; set; }
        public int Maximum { get; set; } = 100;
        public int Value { get; set; }
    }
}
