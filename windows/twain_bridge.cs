using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using NTwain;
using NTwain.Data;

// Run in a separate x86 process: the legacy Windows DSM and many USB sources
// (including Kyocera MA4000x) are 32-bit even on 64-bit Windows.
internal static class TwainBridge
{
    private const int MaximumPages = 40;
    private static readonly ManualResetEvent Finished = new ManualResetEvent(false);
    private static readonly ManualResetEvent PageReceived = new ManualResetEvent(false);
    private static string _error;
    private static int _pages;
    private static string _outputDirectory;

    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        PlatformInfo.Current.PreferNewDSM = false;
        TwainSession session = null;
        DataSource source = null;
        try
        {
            if (args.Length == 0 || (args[0] != "list" && args[0] != "probe" && args[0] != "scan"))
                throw new ArgumentException("Неизвестное действие TWAIN-моста.");
            if (args[0] == "probe" && args.Length != 2)
                throw new ArgumentException("Укажите название TWAIN-источника.");
            if (args[0] == "scan" && args.Length != 6)
                throw new ArgumentException("Для сканирования нужны источник, папка, режим, цвет и DPI.");

            session = new TwainSession(
                TWIdentity.CreateFromAssembly(DataGroups.Image, Assembly.GetExecutingAssembly()));
            session.DataTransferred += OnDataTransferred;
            session.TransferError += (sender, e) =>
            {
                _error = "Ошибка передачи изображения TWAIN: " + e;
                Finished.Set();
            };
            session.SourceDisabled += (sender, e) => Finished.Set();
            if (session.Open() != ReturnCode.Success)
                throw new InvalidOperationException("Не удалось открыть диспетчер TWAIN.");

            if (args[0] == "list")
            {
                var names = new List<string>();
                foreach (var candidate in session)
                {
                    // These are WIA compatibility wrappers, not native TWAIN drivers.
                    if (!candidate.Name.StartsWith("WIA-", StringComparison.OrdinalIgnoreCase))
                        names.Add(candidate.Name);
                }
                WriteJson("[" + String.Join(",", names.ConvertAll(JsonString).ToArray()) + "]");
                return 0;
            }

            foreach (var candidate in session)
            {
                if (String.Equals(candidate.Name, args[1], StringComparison.Ordinal))
                {
                    source = candidate;
                    break;
                }
            }
            if (args[0] == "probe")
            {
                WriteJson("{\"present\":" + (source != null ? "true" : "false") + "}");
                return 0;
            }
            if (source == null)
                throw new InvalidOperationException("Выбранный TWAIN-источник не найден: " + args[1]);
            if (source.Open() != ReturnCode.Success)
                throw new InvalidOperationException("Не удалось открыть выбранный TWAIN-сканер.");

            _outputDirectory = Path.GetFullPath(args[2]);
            Directory.CreateDirectory(_outputDirectory);
            bool feeder = !String.Equals(args[3], "Flatbed", StringComparison.Ordinal);
            if (String.Equals(args[3], "ADF Duplex", StringComparison.Ordinal))
                throw new InvalidOperationException("Двусторонний АПД через TWAIN пока не поддерживается.");
            var feederCap = source.Capabilities.CapFeederEnabled;
            // Set this explicitly even if GetCurrent says True: the Kyocera
            // driver reports stale values for feeder state and paper sensor.
            if (feeder && (!feederCap.CanSet || feederCap.SetValue(BoolType.True) != ReturnCode.Success))
                throw new InvalidOperationException("TWAIN-драйвер не включил автоподатчик.");
            if (!feeder && feederCap.CanGetCurrent && feederCap.GetCurrent() != BoolType.False)
            {
                if (!feederCap.CanSet || feederCap.SetValue(BoolType.False) != ReturnCode.Success)
                {
                    if (source.Capabilities.CapFeederLoaded.GetCurrent() == BoolType.True)
                        throw new InvalidOperationException("TWAIN-драйвер не переключил стекло и автоподатчик. Для сканирования со стекла извлеките листы из АПД.");
                    // Kyocera MA4000x reports the feeder as enabled and refuses
                    // to change that capability, but with an empty feeder it
                    // correctly scans the flatbed.
                }
            }
            if (source.Capabilities.CapXferCount.CanSet)
                source.Capabilities.CapXferCount.SetValue((short)(feeder ? MaximumPages : 1));

            PixelType pixel = args[4] == "Gray" ? PixelType.Gray :
                args[4] == "Lineart" ? PixelType.BlackWhite : PixelType.RGB;
            SetPixelType(source, pixel);
            int requestedDpi;
            if (!Int32.TryParse(args[5], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedDpi))
                throw new ArgumentException("Некорректное разрешение сканирования.");
            int dpi = SelectResolution(source, requestedDpi);
            if (source.Capabilities.ICapXResolution.SetValue((float)dpi) != ReturnCode.Success ||
                source.Capabilities.ICapYResolution.SetValue((float)dpi) != ReturnCode.Success)
                throw new InvalidOperationException("TWAIN-драйвер отклонил разрешение " + dpi + " dpi.");
            if (source.Capabilities.ICapXferMech.SetValue(XferMech.Native) != ReturnCode.Success)
                throw new InvalidOperationException("TWAIN-драйвер не поддерживает передачу изображения.");

            if (source.Enable(SourceEnableMode.NoUI, false, IntPtr.Zero) != ReturnCode.Success)
                throw new InvalidOperationException("TWAIN-драйвер не начал сканирование.");
            int waitMs = feeder ? 300000 : 120000;
            if (feeder && WaitHandle.WaitAny(new WaitHandle[] { PageReceived, Finished }, 25000) == WaitHandle.WaitTimeout)
                throw new TimeoutException("Автоподатчик не передал первую страницу за 25 секунд. Проверьте бумагу и готовность МФУ.");
            if (!Finished.WaitOne(waitMs))
                throw new TimeoutException("TWAIN-сканер не завершил передачу за отведённое время.");
            if (_error != null)
                throw new InvalidOperationException(_error);
            if (_pages == 0)
                throw new InvalidOperationException(feeder ? "В автоподатчике нет документов." : "TWAIN-сканер не передал страницу.");
            // Some drivers report their last page before notifying the DSM of
            // source shutdown. Wait a little for normal cleanup (Kyocera does).
            WriteJson("{\"pages\":" + _pages + ",\"dpi\":" + dpi + "}");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("{\"error\":" + JsonString(ex.Message) + "}");
            return 1;
        }
        finally
        {
            if (source != null)
            {
                try { source.Close(); } catch (Exception) { }
            }
            if (session != null)
            {
                try { session.Close(); } catch (Exception) { }
            }
        }
    }

    private static void OnDataTransferred(object sender, DataTransferredEventArgs e)
    {
        try
        {
            if (_pages >= MaximumPages)
                throw new InvalidOperationException("Достигнут предел 40 страниц за одно TWAIN-задание.");
            using (var image = e.GetNativeImageStream())
            {
                if (image == null)
                    throw new InvalidOperationException("TWAIN-драйвер не передал данные изображения.");
                string target = Path.Combine(_outputDirectory, "raw-" + (_pages + 1).ToString("D4") + ".bmp");
                string partial = target + ".part";
                try
                {
                    using (var file = File.Create(partial))
                        image.CopyTo(file);
                    File.Move(partial, target);
                }
                finally
                {
                    if (File.Exists(partial)) File.Delete(partial);
                }
            }
            _pages++;
            PageReceived.Set();
        }
        catch (Exception ex)
        {
            _error = ex.Message;
            Finished.Set();
        }
    }

    private static void SetPixelType(DataSource source, PixelType requested)
    {
        var cap = source.Capabilities.ICapPixelType;
        if (!cap.CanSet || cap.SetValue(requested) != ReturnCode.Success)
            throw new InvalidOperationException("TWAIN-драйвер отклонил цветовой режим " + requested + ".");
    }

    private static int SelectResolution(DataSource source, int requested)
    {
        if (requested < 75 || requested > 1200)
            throw new ArgumentOutOfRangeException("requested", "Разрешение вне допустимого диапазона.");
        int chosen = -1;
        int distance = Int32.MaxValue;
        foreach (var value in source.Capabilities.ICapXResolution.GetValues())
        {
            int candidate = (int)Math.Round(Convert.ToDouble(value, CultureInfo.InvariantCulture));
            int delta = Math.Abs(candidate - requested);
            if (delta < distance || (delta == distance && candidate < chosen))
            {
                chosen = candidate;
                distance = delta;
            }
        }
        if (chosen < 0)
            throw new InvalidOperationException("TWAIN-драйвер не сообщил поддерживаемые разрешения.");
        return chosen;
    }

    private static void WriteJson(string json)
    {
        Console.WriteLine(json);
        Console.Out.Flush();
    }

    private static string JsonString(string text)
    {
        var output = new StringBuilder("\"");
        foreach (char ch in text ?? "")
        {
            switch (ch)
            {
                case '\\': output.Append("\\\\"); break;
                case '"': output.Append("\\\""); break;
                case '\n': output.Append("\\n"); break;
                case '\r': output.Append("\\r"); break;
                case '\t': output.Append("\\t"); break;
                default:
                    if (ch < 32) output.Append("\\u" + ((int)ch).ToString("X4"));
                    else output.Append(ch);
                    break;
            }
        }
        return output.Append('"').ToString();
    }
}
