// Simulation only (data_layer/PROTOCOL.md section 4): the virtual plate the simulated spectrometer
// looks at. SimulatedDevice loads it when sample.json and reference_spectra.json are found and then
// builds every frame from the simulated stage position, laser state and power level; without the
// files the simulator keeps its synthetic spectrum. Nothing in this file is used with the real device.
//
// virtual_sample/sample_model.py states the same rules in Python; the backend tests compare the two,
// so the arithmetic here is kept in the same order as there.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;

namespace SampleApl
{
    public sealed class VirtualSample
    {
        const int Pixels = 2048;
        const int MaxCount = 65534;                 // one below the ADC's full scale, which readers treat as saturation
        const string SampleSchema = "raman-virtual-sample/1";
        const string ReferenceSchema = "raman-reference-spectra/1";
        const string DarkCapturePrefix = "dark_";   // the controller's Capture Dark: the optical path is blocked

        sealed class Region
        {
            public string Material;
            public bool Frame;                      // otherwise a circle
            public double Inner, Outer;             // frame
            public double CenterX, CenterY, Radius; // circle
        }

        readonly double[] dark;
        readonly Dictionary<string, double[]> signals = new Dictionary<string, double[]>();
        readonly List<Region> regions = new List<Region>();
        readonly List<string> materials = new List<string>();   // region materials in order of appearance, then the background
        readonly string background;
        readonly double frameBlend, circleBlend;
        readonly double focusZ, focusHalfWidth;
        readonly double scaleLow, scaleMedium, scaleHigh;
        readonly double noiseRms;
        readonly double startX, startY, startZ;
        readonly string stageFile;

        public string Folder { get; private set; }

        static string Setting(string name) { return Environment.GetEnvironmentVariable(name); }

        /// <summary>The sample, or null. `problem` stays null when the files are simply not there
        /// (the simulator then behaves as it always did) and says why when they exist but cannot be used.</summary>
        public static VirtualSample TryLoad(out string problem)
        {
            problem = null;
            string folder = Setting("RAMAN_SIM_SAMPLE_DIR");
            if (string.IsNullOrEmpty(folder)) folder = Path.Combine(Directory.GetCurrentDirectory(), "..", "..", "virtual_sample");
            string samplePath = Path.Combine(folder, "sample.json");
            string referencePath = Path.Combine(folder, "reference_spectra.json");
            if (!File.Exists(samplePath) || !File.Exists(referencePath)) return null;
            try
            {
                using (JsonDocument sample = JsonDocument.Parse(File.ReadAllText(samplePath)))
                using (JsonDocument reference = JsonDocument.Parse(File.ReadAllText(referencePath)))
                    return new VirtualSample(Path.GetFullPath(folder), sample.RootElement, reference.RootElement);
            }
            catch (Exception ex)
            {
                problem = ex.Message;
                return null;
            }
        }

        VirtualSample(string folder, JsonElement sample, JsonElement reference)
        {
            const string SampleFile = "sample.json", ReferenceFile = "reference_spectra.json";
            Folder = folder;
            if (Member(sample, "schema", SampleFile).GetString() != SampleSchema) throw new FormatException(SampleFile + ": schema is not " + SampleSchema);
            if (Member(reference, "schema", ReferenceFile).GetString() != ReferenceSchema) throw new FormatException(ReferenceFile + ": schema is not " + ReferenceSchema);

            dark = Spectrum(Member(reference, "dark", ReferenceFile), "dark");
            foreach (JsonProperty signal in Member(reference, "signals", ReferenceFile).EnumerateObject())
                signals[signal.Name] = Spectrum(signal.Value, signal.Name);

            foreach (JsonElement entry in Member(sample, "regions", SampleFile).EnumerateArray())
            {
                Region region = new Region { Material = Member(entry, "material", SampleFile).GetString() };
                string shape = Member(entry, "shape", SampleFile).GetString();
                if (shape == "frame")
                {
                    region.Frame = true;
                    region.Inner = Member(entry, "inner_mm", SampleFile).GetDouble();
                    region.Outer = Member(entry, "outer_mm", SampleFile).GetDouble();
                }
                else if (shape == "circle")
                {
                    JsonElement center = Member(entry, "center_mm", SampleFile);
                    region.CenterX = center[0].GetDouble();
                    region.CenterY = center[1].GetDouble();
                    region.Radius = Member(entry, "radius_mm", SampleFile).GetDouble();
                }
                else throw new FormatException(SampleFile + ": unknown region shape " + shape);
                regions.Add(region);
                if (!materials.Contains(region.Material)) materials.Add(region.Material);
            }
            background = Member(sample, "background", SampleFile).GetString();
            if (!materials.Contains(background)) materials.Add(background);
            foreach (string material in materials)
                if (!signals.ContainsKey(material)) throw new FormatException(ReferenceFile + ": no signal for material " + material);

            JsonElement blend = Member(sample, "edge_blend_mm", SampleFile);
            frameBlend = Optional(blend, "frame");
            circleBlend = Optional(blend, "circle");
            JsonElement focus = Member(sample, "focus", SampleFile);
            focusZ = Member(focus, "z_focus_mm", SampleFile).GetDouble();
            focusHalfWidth = Member(focus, "half_width_mm", SampleFile).GetDouble();
            if (!(focusHalfWidth > 0)) throw new FormatException(SampleFile + ": focus.half_width_mm must be positive");
            JsonElement scale = Member(sample, "power_scale", SampleFile);
            scaleLow = Optional(scale, "LOW");
            scaleMedium = Optional(scale, "MEDIUM");
            scaleHigh = Optional(scale, "HIGH");
            noiseRms = Member(sample, "noise_counts_rms", SampleFile).GetDouble();
            JsonElement start = Member(sample, "start_position_mm", SampleFile);
            startX = Member(start, "x", SampleFile).GetDouble();
            startY = Member(start, "y", SampleFile).GetDouble();
            startZ = Member(start, "z", SampleFile).GetDouble();

            stageFile = Setting("RAMAN_SIM_STAGE_FILE");
            if (string.IsNullOrEmpty(stageFile)) stageFile = Path.Combine(Directory.GetCurrentDirectory(), "..", "_sim_stage.json");
            stageFile = Path.GetFullPath(stageFile);
        }

        // A missing key is named in the message that ends up in the "[sim] virtual sample ignored" line.
        static JsonElement Member(JsonElement parent, string name, string file)
        {
            JsonElement value;
            if (parent.ValueKind != JsonValueKind.Object || !parent.TryGetProperty(name, out value))
                throw new FormatException(file + ": " + name + " is missing");
            return value;
        }

        static double[] Spectrum(JsonElement values, string name)
        {
            if (values.GetArrayLength() != Pixels)
                throw new FormatException("reference_spectra.json: " + name + " has " + values.GetArrayLength().ToString(CultureInfo.InvariantCulture) + " values, not 2048");
            double[] spectrum = new double[Pixels];
            int index = 0;
            foreach (JsonElement value in values.EnumerateArray()) spectrum[index++] = value.GetDouble();
            return spectrum;
        }

        static double Optional(JsonElement parent, string name)
        {
            JsonElement value;
            return parent.TryGetProperty(name, out value) ? value.GetDouble() : 0.0;
        }

        /// <summary>One simulated frame at the current stage position. `ldPower` is the value of the
        /// controller's power command (1 LOW, 2 MEDIUM, 3 HIGH); any other value gives no laser signal.</summary>
        public void Fill(ushort[] image, bool laserOn, long ldPower, string captureId, Random random)
        {
            double gain = 0.0;
            double[] mix = null;
            if (laserOn && !captureId.StartsWith(DarkCapturePrefix, StringComparison.Ordinal))
            {
                double x, y, z;
                StagePosition(out x, out y, out z);
                double scale = ldPower == 1 ? scaleLow : ldPower == 2 ? scaleMedium : ldPower == 3 ? scaleHigh : 0.0;
                double defocus = (z - focusZ) / focusHalfWidth;
                gain = scale * (1.0 / (1.0 + defocus * defocus));
                if (gain != 0.0) mix = Mixture(x, y);
            }
            for (int i = 0; i < Pixels; i++)
            {
                double value = mix == null ? dark[i] : dark[i] + gain * mix[i];
                if (noiseRms > 0) value += noiseRms * Gaussian(random);
                value = Math.Round(value);
                if (value < 0) value = 0;
                if (value > MaxCount) value = MaxCount;
                image[i] = (ushort)value;
            }
        }

        // The motor simulator replaces the stage file atomically, so a read never sees half a file.
        // Before the first simulated move there is none: the stage is then at the start position.
        void StagePosition(out double x, out double y, out double z)
        {
            x = startX; y = startY; z = startZ;
            if (!File.Exists(stageFile)) return;
            try
            {
                using (JsonDocument stage = JsonDocument.Parse(File.ReadAllText(stageFile)))
                {
                    JsonElement root = stage.RootElement;
                    double stepsPerMm = Member(root, "steps_per_mm", "stage file").GetDouble();
                    if (!(stepsPerMm > 0)) throw new FormatException("steps_per_mm must be positive");
                    double xSteps = Member(root, "x_steps", "stage file").GetDouble();
                    double ySteps = Member(root, "y_steps", "stage file").GetDouble();
                    double zSteps = Member(root, "z_steps", "stage file").GetDouble();
                    x = xSteps / stepsPerMm; y = ySteps / stepsPerMm; z = zSteps / stepsPerMm;
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("[sim] stage file " + stageFile + " not usable (" + ex.Message + "): the start position is used");
                Console.Out.Flush();
            }
        }

        // 0 in front of an edge, 1 behind it, linear across the blend width centred on the edge.
        static double Ramp(double inside, double blend)
        {
            if (blend <= 0) return inside >= 0 ? 1.0 : 0.0;
            return Math.Min(1.0, Math.Max(0.0, inside / blend + 0.5));
        }

        double RegionWeight(Region region, double x, double y)
        {
            if (region.Frame)
            {
                double d = Math.Max(x, y);
                return Ramp(d - region.Inner, frameBlend) * Ramp(region.Outer - d, frameBlend);
            }
            double dx = x - region.CenterX, dy = y - region.CenterY;
            return Ramp(region.Radius - Math.Sqrt(dx * dx + dy * dy), circleBlend);
        }

        // Per material the largest weight of its regions; scaled down when they add up to more than 1;
        // the background fills what is left.
        double[] Mixture(double x, double y)
        {
            double[] weights = new double[materials.Count];
            foreach (Region region in regions)
            {
                int index = materials.IndexOf(region.Material);
                weights[index] = Math.Max(weights[index], RegionWeight(region, x, y));
            }
            double total = 0.0;
            for (int m = 0; m < weights.Length; m++) total += weights[m];
            if (total > 1.0)
            {
                for (int m = 0; m < weights.Length; m++) weights[m] /= total;
                total = 1.0;
            }
            weights[materials.IndexOf(background)] += 1.0 - total;

            double[] mix = new double[Pixels];
            for (int m = 0; m < weights.Length; m++)
            {
                if (!(weights[m] > 0.0)) continue;
                double[] signal = signals[materials[m]];
                for (int i = 0; i < Pixels; i++) mix[i] += weights[m] * signal[i];
            }
            return mix;
        }

        // Box-Muller; 1 - NextDouble() is in (0, 1], so the logarithm stays finite.
        static double Gaussian(Random random)
        {
            double u1 = 1.0 - random.NextDouble();
            double u2 = random.NextDouble();
            return Math.Sqrt(-2.0 * Math.Log(u1)) * Math.Cos(2.0 * Math.PI * u2);
        }
    }
}
