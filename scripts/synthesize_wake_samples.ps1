param(
    [string]$OutDir = "$PSScriptRoot\..\assets\wake\positives",
    [int]$Count = 20
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

function Write-Wave(
    [string]$Path,
    [string]$Phrase,
    [string]$Culture,
    [int]$Volume,
    [int]$Rate
) {
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voice = $synth.GetInstalledVoices() |
        Where-Object { $_.VoiceInfo.Culture.Name.StartsWith($Culture) } |
        Select-Object -First 1
    if (-not $voice) {
        throw "No installed System.Speech voice for $Culture"
    }
    $synth.SelectVoice($voice.VoiceInfo.Name)
    $synth.Volume = $Volume
    $synth.Rate = $Rate
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $synth.SetOutputToWaveFile($Path, $format)
    $synth.Speak($Phrase)
    $synth.Dispose()
}

foreach ($language in @(
    @{
        Code = "fr"; Culture = "fr"; Phrase = "Coin-Coin"
        Context = "J'ai entendu Coin-Coin dans le jardin ce matin."
    },
    @{
        Code = "en"; Culture = "en"; Phrase = "Coin-Coin"
        Context = "I heard Coin-Coin in the garden this morning."
    }
)) {
    $directory = Join-Path $OutDir $language.Code
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    foreach ($distance in @("near", "far")) {
        for ($index = 1; $index -le $Count; $index++) {
            $path = Join-Path $directory ("{0}_{1}_{2:D2}.wav" -f $language.Code, $distance, $index)
            $volume = if ($distance -eq "near") { 100 } else { 45 }
            $rate = ($index % 5) - 2
            Write-Wave $path $language.Phrase $language.Culture $volume $rate
        }
    }
    $negativeDirectory = Join-Path ($OutDir -replace "positives$", "negatives") $language.Code
    New-Item -ItemType Directory -Force -Path $negativeDirectory | Out-Null
    for ($index = 1; $index -le 10; $index++) {
        $path = Join-Path $negativeDirectory ("context_{0}_{1:D2}.wav" -f $language.Code, $index)
        Write-Wave $path $language.Context $language.Culture 100 (($index % 5) - 2)
    }
}
Write-Host "Synthetic wake audio written; never use it as a substitute for real recordings."
