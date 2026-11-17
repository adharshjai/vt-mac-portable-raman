// Dumps the public API, P/Invoke declarations, struct layouts and external references of a
// managed assembly. Uses reflection and the metadata reader only; no IL is read.
// Usage: dumpmeta <assembly.dll>            reflection dump (API, structs, P/Invoke signatures)
//        dumpmeta <assembly.dll> --refs     metadata-only dump (assembly/type/member refs, ImplMap, user strings)
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;
using System.Runtime.InteropServices;

if (args.Length > 1 && args[1] == "--refs") { RefDumper.Run(args[0]); return; }
if (args.Length > 2 && args[1] == "--stubdir") {
    var dir = Path.GetFullPath(args[2]);
    System.Runtime.Loader.AssemblyLoadContext.Default.Resolving += (ctx, name) => {
        var candidate = Path.Combine(dir, name.Name + ".dll");
        return File.Exists(candidate) ? ctx.LoadFromAssemblyPath(candidate) : null;
    };
}
ApiDumper.Run(args[0]);

static class ApiDumper {
    static string TypeName(Type t) => t.IsByRef ? "ref " + TypeName(t.GetElementType()) : t.IsArray ? TypeName(t.GetElementType()) + "[]" : t.IsNested ? TypeName(t.DeclaringType) + "." + t.Name : t.FullName ?? t.Name;
    static string Marshal(ICustomAttributeProvider p) { var m = p.GetCustomAttributes(typeof(MarshalAsAttribute), false).OfType<MarshalAsAttribute>().FirstOrDefault(); return m == null ? "" : $" [MarshalAs({m.Value}{(m.SizeConst > 0 ? ", SizeConst=" + m.SizeConst : "")}{(m.ArraySubType != 0 ? ", ArraySubType=" + m.ArraySubType : "")}{(m.SizeParamIndex >= 0 && m.Value == UnmanagedType.LPArray ? ", SizeParamIndex=" + m.SizeParamIndex : "")})]"; }
    public static void Run(string path) {
        var asm = Assembly.LoadFrom(Path.GetFullPath(path));
        var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
        Console.WriteLine($"ASSEMBLY {asm.FullName}  references: {string.Join(", ", asm.GetReferencedAssemblies().Select(r => r.Name))}");
        Type[] types;
        try { types = asm.GetTypes(); }
        catch (ReflectionTypeLoadException ex) {
            types = ex.Types.Where(x => x != null).ToArray();
            foreach (var le in ex.LoaderExceptions.Where(e => e != null).Select(e => e.Message).Distinct()) Console.WriteLine($"LOADER-ERROR {le}");
        }
        foreach (var t in types.OrderBy(t => t.FullName)) {
            try {
                var kind = t.IsEnum ? "enum" : t.IsValueType ? "struct" : t.IsInterface ? "interface" : "class";
                var sl = t.IsValueType && !t.IsEnum ? t.StructLayoutAttribute : null;
                var layout = sl != null ? $" [StructLayout({sl.Value}, Pack={sl.Pack}, Size={sl.Size}, CharSet={sl.CharSet})]" : "";
                Console.WriteLine($"\n{(t.IsPublic || t.IsNestedPublic ? "public " : "")}{kind} {TypeName(t)}{(t.BaseType != null && t.BaseType != typeof(object) && !t.IsValueType ? " : " + TypeName(t.BaseType) : "")}{layout}");
                if (t.IsEnum) { foreach (var n in Enum.GetNames(t)) Console.WriteLine($"    {n} = {Convert.ToInt64(Enum.Parse(t, n))}"); continue; }
                foreach (var f in t.GetFields(flags)) {
                    var off = t.IsValueType ? f.GetCustomAttribute<FieldOffsetAttribute>() : null;
                    Console.WriteLine($"    field {(f.IsPublic ? "public " : "")}{(f.IsStatic ? "static " : "")}{TypeName(f.FieldType)} {f.Name}{(off != null ? " @" + off.Value : "")}{Marshal(f)}{(f.IsLiteral ? " = " + f.GetRawConstantValue() : "")}");
                }
                foreach (var mth in t.GetMethods(flags).Where(m => !m.IsSpecialName).OrderBy(m => m.Name)) {
                    var di = mth.GetCustomAttribute<DllImportAttribute>();
                    var ps = string.Join(", ", mth.GetParameters().Select(p => $"{(p.IsOut && p.ParameterType.IsByRef ? "out " : "")}{TypeName(p.ParameterType)} {p.Name}{Marshal(p)}"));
                    var ret = TypeName(mth.ReturnType) + Marshal(mth.ReturnParameter);
                    if (di != null) Console.WriteLine($"    PINVOKE [DllImport(\"{di.Value}\", EntryPoint=\"{di.EntryPoint ?? mth.Name}\", CharSet={di.CharSet}, SetLastError={di.SetLastError}, CallingConvention={di.CallingConvention}, ExactSpelling={di.ExactSpelling}, PreserveSig={di.PreserveSig})] {ret} {mth.Name}({ps})");
                    else Console.WriteLine($"    {(mth.IsPublic ? "public " : mth.IsPrivate ? "private " : "internal ")}{(mth.IsStatic ? "static " : "")}{ret} {mth.Name}({ps})");
                }
                foreach (var c in t.GetConstructors(flags)) Console.WriteLine($"    ctor({string.Join(", ", c.GetParameters().Select(p => TypeName(p.ParameterType) + " " + p.Name))})");
            } catch (Exception ex) { Console.WriteLine($"    (type dump failed: {ex.GetType().Name}: {ex.Message.Split('\n')[0]})"); }
        }
    }
}

static class RefDumper {
    public static void Run(string path) {
        using var fs = File.OpenRead(path);
        using var pe = new PEReader(fs);
        var md = pe.GetMetadataReader();
        string TypeRefName(TypeReferenceHandle h) { var tr = md.GetTypeReference(h); var ns = md.GetString(tr.Namespace); var name = (ns.Length > 0 ? ns + "." : "") + md.GetString(tr.Name); if (tr.ResolutionScope.Kind == HandleKind.TypeReference) name = TypeRefName((TypeReferenceHandle)tr.ResolutionScope) + "+" + md.GetString(tr.Name); return name; }
        string Scope(EntityHandle h) => h.Kind == HandleKind.AssemblyReference ? md.GetString(md.GetAssemblyReference((AssemblyReferenceHandle)h).Name) : h.Kind == HandleKind.TypeReference ? "nested in " + TypeRefName((TypeReferenceHandle)h) : h.Kind.ToString();
        string TypeDefName(TypeDefinitionHandle h) { var td = md.GetTypeDefinition(h); var ns = md.GetString(td.Namespace); return (ns.Length > 0 ? ns + "." : "") + md.GetString(td.Name); }
        Console.WriteLine("== assembly references ==");
        foreach (var ah in md.AssemblyReferences) { var a = md.GetAssemblyReference(ah); Console.WriteLine($"  {md.GetString(a.Name)} {a.Version} token={Convert.ToHexString(md.GetBlobBytes(a.PublicKeyOrToken))}"); }
        Console.WriteLine("== type references ==");
        foreach (var th in md.TypeReferences) { var tr = md.GetTypeReference(th); Console.WriteLine($"  [{Scope(tr.ResolutionScope)}] {TypeRefName(th)}"); }
        Console.WriteLine("== member references (external members used by this assembly) ==");
        foreach (var mh in md.MemberReferences) {
            var mr = md.GetMemberReference(mh);
            var parent = mr.Parent.Kind == HandleKind.TypeReference ? TypeRefName((TypeReferenceHandle)mr.Parent) : mr.Parent.Kind == HandleKind.TypeSpecification ? "typespec" : mr.Parent.Kind.ToString();
            var blob = md.GetBlobReader(mr.Signature); var header = blob.ReadSignatureHeader();
            string kind = mr.GetKind() == MemberReferenceKind.Field ? "field" : "method";
            int paramCount = kind == "method" ? (header.IsGeneric ? blob.ReadCompressedInteger() : 0) * 0 + blob.ReadCompressedInteger() : 0;
            Console.WriteLine($"  {kind} {parent}::{md.GetString(mr.Name)}{(kind == "method" ? $" ({paramCount} params{(header.IsInstance ? ", instance" : ", static")})" : "")}");
        }
        Console.WriteLine("== type definitions ==");
        foreach (var th in md.TypeDefinitions) { var td = md.GetTypeDefinition(th); var bt = td.BaseType.IsNil ? "" : td.BaseType.Kind == HandleKind.TypeReference ? " : " + TypeRefName((TypeReferenceHandle)td.BaseType) : td.BaseType.Kind == HandleKind.TypeDefinition ? " : " + TypeDefName((TypeDefinitionHandle)td.BaseType) : ""; Console.WriteLine($"  {TypeDefName(th)}{bt}  [{td.Attributes}] fields={td.GetFields().Count} methods={td.GetMethods().Count}"); }
        Console.WriteLine("== P/Invoke (ImplMap) ==");
        foreach (var mh in md.MethodDefinitions) { var m = md.GetMethodDefinition(mh); var im = m.GetImport(); if (im.Module.IsNil) continue; var mod = md.GetModuleReference(im.Module); Console.WriteLine($"  {md.GetString(mod.Name)} :: {md.GetString(im.Name)}  managed={md.GetString(m.Name)}  attrs={im.Attributes}"); }
        Console.WriteLine("== user strings ==");
        var h = MetadataTokens.UserStringHandle(1);
        while (!h.IsNil) { var s = md.GetUserString(h); if (s.Length > 0) Console.WriteLine("  " + s.Replace("\n", "\\n").Replace("\r", "\\r")); h = md.GetNextHandle(h); }
    }
}
