<template>
  <el-row :gutter="12">
    <el-col :span="15"><el-input v-model="model.url" placeholder="https://files.example.com/file.docx" /></el-col>
    <el-col :span="9"><el-input v-model="model.file_name" placeholder="文件名.docx" /></el-col>
  </el-row>
</template>

<script setup lang="ts">
const model = defineModel<{ url: string; file_name: string }>({ required: true })
</script>

