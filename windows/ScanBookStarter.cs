// Startprogramm fuer Windows: ScanBook.exe startet die Browser-Oberflaeche
// mit dem mitgelieferten Python (Ordner "python") oder einem installierten.
// Wird vom Workflow "Windows-Paket" mit csc.exe uebersetzt.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("ScanBook")]
[assembly: System.Reflection.AssemblyProduct("ScanBook")]

static class ScanBookStarter
{
    static int Main()
    {
        string ordner = AppDomain.CurrentDomain.BaseDirectory;
        string skript = Path.Combine(ordner, "ScanBook_Web.py");

        if (!File.Exists(skript))
        {
            Fehler("ScanBook_Web.py wurde nicht gefunden.\n\n" +
                   "Bitte die ZIP-Datei zuerst entpacken (Rechtsklick → „Alle extrahieren“) " +
                   "und ScanBook.exe im entpackten Ordner starten.");
            return 1;
        }

        string mitgeliefert = Path.Combine(ordner, "python", "python.exe");
        string[][] kandidaten = File.Exists(mitgeliefert)
            ? new[] { new[] { mitgeliefert, "" } }
            : new[] { new[] { "py", "-3 " }, new[] { "python", "" } };

        foreach (string[] k in kandidaten)
        {
            var info = new ProcessStartInfo(k[0], k[1] + "\"" + skript + "\"")
            {
                WorkingDirectory = ordner,
                UseShellExecute = false,
            };
            Process p;
            try
            {
                p = Process.Start(info);
            }
            catch (System.ComponentModel.Win32Exception)
            {
                continue; // Programm nicht vorhanden, naechsten Kandidaten versuchen
            }
            p.WaitForExit();
            if (p.ExitCode != 0)
            {
                Console.WriteLine();
                Console.WriteLine("ScanBook wurde mit einem Fehler beendet. Enter druecken zum Schliessen.");
                Console.ReadLine();
            }
            return p.ExitCode;
        }

        Fehler("Python wurde nicht gefunden.\n\n" +
               "Bitte das Paket „ScanBook-Windows.zip“ verwenden, es bringt Python schon mit.");
        return 1;
    }

    static void Fehler(string text)
    {
        MessageBox.Show(text, "ScanBook", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
