using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class WiaBridge
{
    private const int ScannerDeviceType = 1;
    private const int PropertyDeviceDescription = 4;
    private const int PropertyPortName = 6;
    private const int PropertyDeviceName = 7;
    private const int PropertyItemCategory = 1029;
    private const int PropertyDocumentHandlingCapabilities = 3086;
    private const int PropertyDocumentHandlingSelect = 3088;
    private const int PropertyPages = 3096;
    private const int PropertyIntent = 6146;
    private const int PropertyXResolution = 6147;
    private const int PropertyYResolution = 6148;
    private const int PropertyXPosition = 6149;
    private const int PropertyYPosition = 6150;
    private const int PropertyXExtent = 6151;
    private const int PropertyYExtent = 6152;

    private const int SelectFeeder = 1;
    private const int SelectFlatbed = 2;
    private const int SelectDuplex = 4;
    private const int CapabilityFeeder = 1;
    private const int CapabilityDuplex = 4;
    private const int IntentColor = 1;
    private const int IntentGrayscale = 2;
    private const int IntentText = 4;

    private const string FormatBmp = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}";
    private const string CategoryFlatbed = "{FB607B1F-43F3-488B-855B-FB703EC342A6}";
    private const string CategoryFeeder = "{FE131934-F84C-42AD-8DA4-6129CDDD7288}";
    private const string ErrorPaperEmpty = "0x80210003";
    private const string StatusEndOfMedia = "0x00210001";

    [STAThread]
    private static int Main(string[] arguments)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Dictionary<string, string> options = ParseArguments(arguments);
        string action = GetOption(options, "action", "").ToLowerInvariant();
        try
        {
            if (action == "selftest")
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "ok", true },
                    { "bridge", "wia-native" }
                });
                return 0;
            }
            if (action != "list" && action != "probe" && action != "scan")
            {
                throw new InvalidOperationException("Неизвестное действие WIA-моста.");
            }

            Type managerType = Type.GetTypeFromProgID("WIA.DeviceManager");
            if (managerType == null)
            {
                throw new InvalidOperationException("Компонент Windows WIA не зарегистрирован.");
            }
            dynamic manager = Activator.CreateInstance(managerType);

            if (action == "list")
            {
                WriteJson(ListDevices(manager));
                return 0;
            }

            string deviceId = GetOption(options, "deviceid", "");
            if (String.IsNullOrWhiteSpace(deviceId))
            {
                throw new InvalidOperationException(
                    "Не указан идентификатор выбранного WIA-сканера."
                );
            }
            dynamic deviceInfo = GetDeviceInfo(manager, deviceId);
            if (deviceInfo == null)
            {
                throw new InvalidOperationException(
                    "Выбранный WIA-сканер больше не зарегистрирован в Windows."
                );
            }

            if (action == "probe")
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "present", true },
                    { "device_id", deviceId }
                });
                return 0;
            }

            string outputDirectory = GetOption(options, "outputdirectory", "");
            if (String.IsNullOrWhiteSpace(outputDirectory))
            {
                throw new InvalidOperationException("Не указан каталог для полученных страниц.");
            }
            string source = GetOption(options, "source", "ADF");
            string mode = GetOption(options, "mode", "Color");
            string paper = GetOption(options, "paper", "A4");
            int dpi = ParseInt(GetOption(options, "dpi", "300"), 300);
            WriteJson(Scan(deviceInfo, outputDirectory, source, mode, dpi, paper));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(Json(new Dictionary<string, object>
            {
                { "error", InnermostMessage(error) },
                { "hresult", HResultHex(error) }
            }));
            return 1;
        }
    }

    private static Dictionary<string, string> ParseArguments(string[] arguments)
    {
        Dictionary<string, string> result = new Dictionary<string, string>(
            StringComparer.OrdinalIgnoreCase
        );
        for (int index = 0; index < arguments.Length; index++)
        {
            string key = arguments[index].TrimStart('-');
            if (String.IsNullOrWhiteSpace(key))
            {
                continue;
            }
            string value = "true";
            if (index + 1 < arguments.Length && !arguments[index + 1].StartsWith("-"))
            {
                value = arguments[++index];
            }
            result[key] = value;
        }
        return result;
    }

    private static string GetOption(
        Dictionary<string, string> options,
        string key,
        string fallback
    )
    {
        string value;
        return options.TryGetValue(key, out value) ? value : fallback;
    }

    private static int ParseInt(string value, int fallback)
    {
        int parsed;
        return Int32.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)
            ? parsed
            : fallback;
    }

    private static List<object> ListDevices(dynamic manager)
    {
        List<object> devices = new List<object>();
        foreach (dynamic info in manager.DeviceInfos)
        {
            if (Convert.ToInt32(info.Type, CultureInfo.InvariantCulture) != ScannerDeviceType)
            {
                continue;
            }
            devices.Add(new Dictionary<string, object>
            {
                { "device_id", Convert.ToString(info.DeviceID, CultureInfo.InvariantCulture) },
                { "name", PropertyText(info.Properties, PropertyDeviceName, "Сканер WIA") },
                { "description", PropertyText(info.Properties, PropertyDeviceDescription, "") },
                { "port", PropertyText(info.Properties, PropertyPortName, "") },
                { "capabilities_known", false },
                { "has_adf", false },
                { "has_flatbed", false },
                { "has_duplex", false },
                { "resolutions", new List<int>() }
            });
        }
        return devices;
    }

    private static dynamic GetDeviceInfo(dynamic manager, string wantedId)
    {
        foreach (dynamic info in manager.DeviceInfos)
        {
            if (Convert.ToInt32(info.Type, CultureInfo.InvariantCulture) != ScannerDeviceType)
            {
                continue;
            }
            string id = Convert.ToString(info.DeviceID, CultureInfo.InvariantCulture);
            if (String.Equals(id, wantedId, StringComparison.Ordinal))
            {
                return info;
            }
        }
        return null;
    }

    private static dynamic GetProperty(dynamic properties, int propertyId)
    {
        if (properties == null)
        {
            return null;
        }
        foreach (dynamic property in properties)
        {
            if (Convert.ToInt32(property.PropertyID, CultureInfo.InvariantCulture) == propertyId)
            {
                return property;
            }
        }
        return null;
    }

    private static object PropertyValue(dynamic properties, int propertyId, object fallback)
    {
        dynamic property = GetProperty(properties, propertyId);
        if (property == null)
        {
            return fallback;
        }
        try
        {
            object value = property.Value;
            return value ?? fallback;
        }
        catch
        {
            return fallback;
        }
    }

    private static string PropertyText(dynamic properties, int propertyId, string fallback)
    {
        object value = PropertyValue(properties, propertyId, fallback);
        return Convert.ToString(value, CultureInfo.InvariantCulture) ?? fallback;
    }

    private static int PropertyInt(dynamic properties, int propertyId, int fallback)
    {
        object value = PropertyValue(properties, propertyId, fallback);
        try
        {
            return Convert.ToInt32(value, CultureInfo.InvariantCulture);
        }
        catch
        {
            return fallback;
        }
    }

    private static int NearestPropertyValue(dynamic property, int requested)
    {
        try
        {
            List<int> values = new List<int>();
            foreach (dynamic item in property.SubTypeValues)
            {
                values.Add(Convert.ToInt32(item, CultureInfo.InvariantCulture));
            }
            if (values.Count > 0)
            {
                values.Sort();
                int selected = values[0];
                foreach (int value in values)
                {
                    if (value <= requested)
                    {
                        selected = value;
                    }
                }
                return selected;
            }
        }
        catch
        {
        }

        try
        {
            int minimum = Convert.ToInt32(property.SubTypeMin, CultureInfo.InvariantCulture);
            int maximum = Convert.ToInt32(property.SubTypeMax, CultureInfo.InvariantCulture);
            int step = Math.Max(
                1,
                Convert.ToInt32(property.SubTypeStep, CultureInfo.InvariantCulture)
            );
            int bounded = Math.Min(maximum, Math.Max(minimum, requested));
            return minimum + (((bounded - minimum) / step) * step);
        }
        catch
        {
            return requested;
        }
    }

    private static int? SetProperty(
        dynamic properties,
        int propertyId,
        int value,
        bool chooseSupported
    )
    {
        dynamic property = GetProperty(properties, propertyId);
        if (property == null)
        {
            return null;
        }
        int effective = chooseSupported ? NearestPropertyValue(property, value) : value;
        try
        {
            property.Value = effective;
            return effective;
        }
        catch
        {
            return null;
        }
    }

    private static dynamic GetTransferItem(dynamic device, string source)
    {
        string wantedCategory = source == "Flatbed" ? CategoryFlatbed : CategoryFeeder;
        dynamic fallback = null;
        foreach (dynamic item in device.Items)
        {
            if (fallback == null)
            {
                fallback = item;
            }
            string category = PropertyText(item.Properties, PropertyItemCategory, "");
            if (String.Equals(category, wantedCategory, StringComparison.OrdinalIgnoreCase))
            {
                return item;
            }
        }
        return fallback;
    }

    private static int SetScanProperties(
        dynamic device,
        dynamic item,
        string source,
        string mode,
        int requestedDpi,
        string paper
    )
    {
        int selection = source == "Flatbed" ? SelectFlatbed : SelectFeeder;
        if (source == "ADF Duplex")
        {
            selection |= SelectDuplex;
        }
        int? deviceSelection = SetProperty(
            device.Properties,
            PropertyDocumentHandlingSelect,
            selection,
            false
        );
        int? itemSelection = SetProperty(
            item.Properties,
            PropertyDocumentHandlingSelect,
            selection,
            false
        );
        string category = PropertyText(item.Properties, PropertyItemCategory, "");
        if (source != "Flatbed" && !deviceSelection.HasValue && !itemSelection.HasValue &&
            !String.Equals(category, CategoryFeeder, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Драйвер WIA не позволяет выбрать автоподатчик.");
        }

        int intent = mode == "Gray"
            ? IntentGrayscale
            : (mode == "Lineart" ? IntentText : IntentColor);
        SetProperty(item.Properties, PropertyIntent, intent, false);
        int? effectiveX = SetProperty(
            item.Properties,
            PropertyXResolution,
            requestedDpi,
            true
        );
        int? effectiveY = SetProperty(
            item.Properties,
            PropertyYResolution,
            requestedDpi,
            true
        );
        int effectiveDpi = effectiveX ?? effectiveY ?? requestedDpi;

        SetProperty(item.Properties, PropertyXPosition, 0, false);
        SetProperty(item.Properties, PropertyYPosition, 0, false);
        double widthInches = paper == "Letter" ? 8.5 : 210.0 / 25.4;
        double heightInches = paper == "Letter" ? 11.0 : 297.0 / 25.4;
        int widthPixels = (int)Math.Floor(widthInches * effectiveDpi);
        int heightPixels = (int)Math.Floor(heightInches * effectiveDpi);
        SetProperty(item.Properties, PropertyXExtent, widthPixels, true);
        SetProperty(item.Properties, PropertyYExtent, heightPixels, true);
        if (source != "Flatbed")
        {
            SetProperty(device.Properties, PropertyPages, 1, false);
            SetProperty(item.Properties, PropertyPages, 1, false);
        }
        return effectiveDpi;
    }

    private static Dictionary<string, object> Scan(
        dynamic deviceInfo,
        string outputDirectory,
        string source,
        string mode,
        int dpi,
        string paper
    )
    {
        Directory.CreateDirectory(outputDirectory);
        foreach (string path in Directory.GetFiles(outputDirectory, "raw-*.bmp"))
        {
            File.Delete(path);
        }

        dynamic device = deviceInfo.Connect();
        dynamic item = GetTransferItem(device, source);
        if (item == null)
        {
            throw new InvalidOperationException(
                "Драйвер WIA не предоставил элемент для получения изображения."
            );
        }
        int capabilities = PropertyInt(
            device.Properties,
            PropertyDocumentHandlingCapabilities,
            0
        );
        if (source != "Flatbed" && capabilities != 0 &&
            (capabilities & CapabilityFeeder) == 0)
        {
            throw new InvalidOperationException(
                "Выбранный сканер не сообщает о наличии автоподатчика."
            );
        }
        if (source == "ADF Duplex" && capabilities != 0 &&
            (capabilities & CapabilityDuplex) == 0)
        {
            throw new InvalidOperationException(
                "Выбранный сканер не поддерживает аппаратный дуплекс WIA."
            );
        }

        int effectiveDpi = SetScanProperties(device, item, source, mode, dpi, paper);
        List<string> files = new List<string>();
        int maximumImages = source == "Flatbed" ? 1 : 500;
        for (int index = 1; index <= maximumImages; index++)
        {
            try
            {
                dynamic image = item.Transfer(FormatBmp);
                if (image == null)
                {
                    if (files.Count > 0)
                    {
                        break;
                    }
                    throw new InvalidOperationException("WIA не вернул изображение.");
                }
                string path = Path.Combine(
                    outputDirectory,
                    String.Format(CultureInfo.InvariantCulture, "raw-{0:D4}.bmp", index)
                );
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
                image.SaveFile(path);
                files.Add(path);
            }
            catch (Exception error)
            {
                string errorCode = HResultHex(error);
                if (files.Count > 0 && source != "Flatbed" &&
                    (errorCode == ErrorPaperEmpty || errorCode == StatusEndOfMedia))
                {
                    break;
                }
                throw;
            }
            if (source == "Flatbed")
            {
                break;
            }
        }

        if (files.Count == 0)
        {
            throw new InvalidOperationException("WIA не получил ни одной страницы.");
        }
        return new Dictionary<string, object>
        {
            { "files", files },
            { "pages", files.Count },
            { "effective_dpi", effectiveDpi },
            { "source", source }
        };
    }

    private static string InnermostMessage(Exception error)
    {
        Exception current = error;
        while (current.InnerException != null)
        {
            current = current.InnerException;
        }
        return String.IsNullOrWhiteSpace(current.Message)
            ? "Неизвестная ошибка Windows WIA."
            : current.Message;
    }

    private static string HResultHex(Exception error)
    {
        Exception current = error;
        while (current.InnerException != null)
        {
            current = current.InnerException;
        }
        int signed = Marshal.GetHRForException(current);
        return "0x" + unchecked((uint)signed).ToString("X8", CultureInfo.InvariantCulture);
    }

    private static void WriteJson(object value)
    {
        Console.WriteLine(Json(value));
    }

    private static string Json(object value)
    {
        if (value == null)
        {
            return "null";
        }
        string text = value as string;
        if (text != null)
        {
            return JsonString(text);
        }
        if (value is bool)
        {
            return (bool)value ? "true" : "false";
        }
        IDictionary dictionary = value as IDictionary;
        if (dictionary != null)
        {
            List<string> members = new List<string>();
            foreach (DictionaryEntry entry in dictionary)
            {
                members.Add(JsonString(Convert.ToString(entry.Key)) + ":" + Json(entry.Value));
            }
            return "{" + String.Join(",", members.ToArray()) + "}";
        }
        IEnumerable sequence = value as IEnumerable;
        if (sequence != null)
        {
            List<string> items = new List<string>();
            foreach (object item in sequence)
            {
                items.Add(Json(item));
            }
            return "[" + String.Join(",", items.ToArray()) + "]";
        }
        return Convert.ToString(value, CultureInfo.InvariantCulture);
    }

    private static string JsonString(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        foreach (char character in value ?? "")
        {
            switch (character)
            {
                case '\\': result.Append("\\\\"); break;
                case '\"': result.Append("\\\""); break;
                case '\b': result.Append("\\b"); break;
                case '\f': result.Append("\\f"); break;
                case '\n': result.Append("\\n"); break;
                case '\r': result.Append("\\r"); break;
                case '\t': result.Append("\\t"); break;
                default:
                    if (character < 32)
                    {
                        result.Append("\\u");
                        result.Append(((int)character).ToString("X4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        result.Append(character);
                    }
                    break;
            }
        }
        result.Append('\"');
        return result.ToString();
    }
}
